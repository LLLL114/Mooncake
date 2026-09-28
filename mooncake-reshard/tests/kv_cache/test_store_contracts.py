"""Shared Store contracts, fixed-page byte layouts and bidirectional TP/PP joins."""

from __future__ import annotations

import json
import struct
from dataclasses import fields, replace

import pytest
from mooncake.reshard.kv_cache import (
    KVCacheComponent,
    KVCacheStoreFormat,
    KVCacheStoreLoadPlan,
    KVCacheStoreManifest,
    KVCacheStorePlanningLimits,
    KVCacheStoreShard,
    KVCacheStoreUploadPlan,
    assemble_kv_cache_placement,
    kv_cache_part_from_json,
    kv_cache_part_to_json,
    kv_cache_store_layout_from_json,
    kv_cache_store_layout_to_json,
    kv_cache_store_manifest_from_json,
    kv_cache_store_manifest_to_json,
    plan_kv_cache_store_load,
    plan_kv_cache_store_upload,
)
from test_kv_cache_reshard import _placement

FORMATS = tuple(KVCacheStoreFormat)


def _layout(tp=1, layers=((0, 1), (2, 3)), fmt=FORMATS[0]):
    return plan_kv_cache_store_upload(
        _placement("first", layers, tp), object_format=fmt
    ).layout


def _manifest(layout=None):
    return KVCacheStoreManifest(
        "cache", "model", "revision", "semantic", layout or _layout()
    )


def _offset(fmt, token, layer, head, layers, heads, tokens, head_bytes):
    # Independent row-major tensor indexing; do not use production strides.
    if fmt is KVCacheStoreFormat.PLHD:
        return ((token * layers + layer) * heads + head) * head_bytes
    if fmt is KVCacheStoreFormat.LPHD:
        return ((layer * tokens + token) * heads + head) * head_bytes
    return ((head * tokens + token) * layers + layer) * head_bytes


def _reference(layout, layer_ids, head_start, head_count, component):
    d = layout.descriptor
    dim = d.key_head_dim if component is KVCacheComponent.KEY else d.value_head_dim
    result = bytearray(len(layer_ids) * d.page_size * head_count * dim * d.itemsize)
    for t in range(d.page_size):
        for local_layer, layer in enumerate(layer_ids):
            for h in range(head_count):
                off = _offset(
                    layout.object_format,
                    t,
                    local_layer,
                    h,
                    len(layer_ids),
                    head_count,
                    d.page_size,
                    dim * d.itemsize,
                )
                for x in range(dim):
                    value = layer * 1000 + t * 50 + (head_start + h) * 8 + x
                    if component is KVCacheComponent.VALUE:
                        value += 20000
                    struct.pack_into("<H", result, off + 2 * x, value)
    return result


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("source_tp", [1, 2, 4, 8])
@pytest.mark.parametrize("target_tp", [1, 2, 8])
def test_source_native_upload_and_load_match_independent_physical_buffers(
    fmt, source_tp, target_tp
):
    # Both TP and PP boundaries change. TP8 also exercises replicated GQA heads.
    source = _placement("writer", ((0, 1), (2, 3)), source_tp, dp_size=2)
    target = _placement("reader", ((0,), (1, 2), (3,)), target_tp, dp_size=2)
    upload = plan_kv_cache_store_upload(source, object_format=fmt, source_dp_rank=1)
    layout = upload.layout
    objects = [bytearray(size) for size in layout.object_sizes]
    written = [set() for _ in objects]
    for f in layout.fragments:
        part = source.part(upload.object_writers[f.object_index])
        assert part.rank.dp == 1 and part.replica_ordinal == 0
        raw = _reference(
            layout, part.layer_ids, part.head_start, part.head_count, f.component
        )
        dim = (
            layout.descriptor.key_head_dim
            if f.component is KVCacheComponent.KEY
            else layout.descriptor.value_head_dim
        )
        head_bytes = dim * 2
        for t in range(layout.token_count):
            for h in range(f.head_count):
                src = _offset(
                    fmt,
                    t,
                    part.layer_ids.index(f.global_layer_id),
                    f.head_start + h - part.head_start,
                    len(part.layer_ids),
                    part.head_count,
                    layout.token_count,
                    head_bytes,
                )
                dst = (
                    f.object_offset + t * f.token_stride_bytes + h * f.head_stride_bytes
                )
                indices = set(range(dst, dst + head_bytes))
                assert not written[f.object_index] & indices
                written[f.object_index].update(indices)
                objects[f.object_index][dst : dst + head_bytes] = raw[
                    src : src + head_bytes
                ]
    for index, raw in enumerate(objects):
        shard = layout.shards[index // 2]
        component = tuple(KVCacheComponent)[index % 2]
        assert written[index] == set(range(len(raw)))
        assert raw == _reference(
            layout, shard.layer_ids, shard.head_start, shard.head_count, component
        )

    load = plan_kv_cache_store_load(
        layout, target, runtime_format=fmt, target_dp_rank=1
    )
    restored = {}
    touched = {}
    for item in load.ranges:
        f = item.fragment
        part = target.part(item.participant_id)
        assert part.rank.dp == 1
        key = (part.participant_id, f.component)
        expected = _reference(
            layout, part.layer_ids, part.head_start, part.head_count, f.component
        )
        raw = restored.setdefault(key, bytearray(len(expected)))
        seen = touched.setdefault(key, set())
        dim = (
            layout.descriptor.key_head_dim
            if f.component is KVCacheComponent.KEY
            else layout.descriptor.value_head_dim
        )
        head_bytes = dim * 2
        for t in range(layout.token_count):
            for h in range(f.head_count):
                src = (
                    f.object_offset + t * f.token_stride_bytes + h * f.head_stride_bytes
                )
                dst = _offset(
                    fmt,
                    t,
                    part.layer_ids.index(f.global_layer_id),
                    f.head_start + h - part.head_start,
                    len(part.layer_ids),
                    part.head_count,
                    layout.token_count,
                    head_bytes,
                )
                indices = set(range(dst, dst + head_bytes))
                assert not seen & indices
                seen.update(indices)
                raw[dst : dst + head_bytes] = objects[f.object_index][
                    src : src + head_bytes
                ]
    for part in target.parts:
        if part.rank.dp == 1:
            for component in KVCacheComponent:
                key = (part.participant_id, component)
                expected = _reference(
                    layout, part.layer_ids, part.head_start, part.head_count, component
                )
                assert restored[key] == expected
                assert touched[key] == set(range(len(expected)))


@pytest.mark.parametrize("fmt", FORMATS)
def test_tp1pp3_to_tp2pp2_read_splits_layers_and_heads(fmt):
    layout = _layout(1, (tuple(range(4)), tuple(range(4, 8)), tuple(range(8, 12))), fmt)
    target = _placement("reader", (tuple(range(6)), tuple(range(6, 12))), 2)
    plan = plan_kv_cache_store_load(layout, target, runtime_format=fmt)
    local = [r.fragment for r in plan.ranges if r.participant_id == "reader-p0-t0"]
    assert {f.global_layer_id for f in local} == set(range(6))
    assert all((f.head_start, f.head_count) == (0, 2) for f in local)
    assert {f.object_index for f in local} == {0, 1, 2, 3}


def test_shared_domain_excludes_layout_but_includes_semantics_and_format():
    first = _manifest(_layout(1))
    other = _manifest(_layout(2, ((0, 1, 2, 3),)))
    assert first.layout != other.layout
    assert first.manifest_key != other.manifest_key
    assert set(first.object_keys("page")).isdisjoint(other.object_keys("page"))
    assert first.model_domain == other.model_domain
    assert first.digest != other.digest
    for name in ("namespace", "model_id", "model_revision", "semantic_fingerprint"):
        assert replace(first, **{name: "different"}).model_domain != first.model_domain
    assert (
        replace(first, layout=_layout(fmt=FORMATS[1])).model_domain
        != first.model_domain
    )


def test_page_identity_needs_no_snapshot_or_per_page_manifest():
    manifest = _manifest()
    wire = kv_cache_store_manifest_to_json(manifest)
    assert (
        "snapshot" not in wire
        and "page_key" not in wire
        and "storage_generation" not in wire
    )
    assert "snapshot" not in {f.name for f in fields(KVCacheStoreLoadPlan)}
    assert (
        manifest.manifest_key
        == f"kv/{manifest.model_domain}/manifests/{manifest.layout.digest}"
    )
    a = manifest.object_keys("prefix/a")
    b = manifest.object_keys("prefix%2Fa")
    assert a != b
    assert a[0] == f"prefix/a_l{manifest.layout.digest}_0_0_k"
    assert a[1] == f"prefix/a_l{manifest.layout.digest}_0_0_v"
    assert len(a) == len(manifest.layout.object_sizes)
    assert manifest.object_keys("another-page") != a
    assert wire == kv_cache_store_manifest_to_json(manifest)
    assert manifest.layout.token_count == manifest.layout.descriptor.page_size
    with pytest.raises(ValueError):
        manifest.object_keys("")
    with pytest.raises(TypeError):
        plan_kv_cache_store_upload(
            _placement("s", ((0,),), 1), object_format=FORMATS[0], token_count=3
        )


def test_layout_is_independent_of_worker_names_and_selected_dp():
    a = plan_kv_cache_store_upload(
        _placement("a", ((0, 1),), 2, dp_size=2), object_format=FORMATS[0]
    )
    b = plan_kv_cache_store_upload(
        _placement("b", ((0, 1),), 2, dp_size=2),
        object_format=FORMATS[0],
        source_dp_rank=1,
    )
    assert a.layout == b.layout and a.layout.digest == b.layout.digest
    assert a.object_writers != b.object_writers
    assert replace(a.layout, shards=tuple(reversed(a.layout.shards))) == a.layout


@pytest.mark.parametrize("fmt", FORMATS)
def test_k_and_v_dimensions_produce_independent_object_sizes(fmt):
    layout = _layout(fmt=fmt)
    layout = replace(layout, descriptor=replace(layout.descriptor, value_head_dim=4))
    for index in range(0, len(layout.object_sizes), 2):
        assert layout.object_sizes[index] == 2 * layout.object_sizes[index + 1]
    for f in layout.fragments:
        dim = (
            layout.descriptor.key_head_dim if f.component is KVCacheComponent.KEY else 4
        )
        end = (
            f.object_offset
            + (layout.token_count - 1) * f.token_stride_bytes
            + (f.head_count - 1) * f.head_stride_bytes
            + dim * 2
        )
        assert end <= layout.object_sizes[f.object_index]


@pytest.mark.parametrize(
    "shards",
    [
        (),
        (KVCacheStoreShard((0,), 0, 4, "slot-1"),),
        (KVCacheStoreShard((0, 1, 2, 3), 0, 3, "slot-2"),),
        (KVCacheStoreShard((0, 1, 2, 3), 0, 5, "slot-3"),),
        (KVCacheStoreShard((0, 1, 2, 3, 99), 0, 4, "slot-4"),),
        (
            KVCacheStoreShard((0, 1, 2, 3), 0, 4, "slot-5"),
            KVCacheStoreShard((0,), 0, 1, "slot-6"),
        ),
    ],
)
def test_invalid_logical_coverage_is_rejected(shards):
    with pytest.raises(ValueError):
        replace(_layout(), shards=shards)


@pytest.mark.parametrize(
    "layers,start,count",
    [((0, 0), 0, 1), ((True,), 0, 1), ((0,), True, 1), ((0,), 0, 0), ((0,), -1, 1)],
)
def test_invalid_shard_fields(layers, start, count):
    with pytest.raises(ValueError):
        KVCacheStoreShard(layers, start, count, "slot-0")


@pytest.mark.parametrize(
    "limit",
    [
        KVCacheStorePlanningLimits(max_objects=1),
        KVCacheStorePlanningLimits(max_fragments=1),
        KVCacheStorePlanningLimits(max_bytes=1),
    ],
)
def test_planning_limits(limit):
    with pytest.raises(ValueError, match="limit"):
        plan_kv_cache_store_upload(
            _placement("s", ((0, 1),), 2), object_format=FORMATS[0], limits=limit
        )
    with pytest.raises(ValueError, match="limit"):
        plan_kv_cache_store_load(
            _layout(),
            _placement("s", ((0, 1), (2, 3)), 2),
            runtime_format=FORMATS[0],
            limits=limit,
        )


def test_load_replica_expansion_obeys_byte_budget():
    layout = _layout()
    runtime = _placement("s", ((0, 1), (2, 3)), 8)
    limits = KVCacheStorePlanningLimits(max_bytes=sum(layout.object_sizes))
    plan_kv_cache_store_upload(runtime, object_format=FORMATS[0], limits=limits)
    with pytest.raises(ValueError, match="byte limit"):
        plan_kv_cache_store_load(
            layout, runtime, runtime_format=FORMATS[0], limits=limits
        )


def test_plan_identity_and_unsupported_conversions():
    layout = _layout()
    runtime = _placement("s", ((0, 1), (2, 3)), 2)
    load = plan_kv_cache_store_load(layout, runtime, runtime_format=FORMATS[0])
    assert (
        load.digest
        == plan_kv_cache_store_load(layout, runtime, runtime_format=FORMATS[0]).digest
    )
    with pytest.raises(TypeError):
        plan_kv_cache_store_upload(runtime, layout, object_format=FORMATS[0])
    with pytest.raises(ValueError, match="cross-pool"):
        plan_kv_cache_store_load(layout, runtime, runtime_format=FORMATS[1])
    with pytest.raises(ValueError, match="descriptors differ"):
        plan_kv_cache_store_load(
            layout,
            _placement("s", ((0, 1), (2, 3)), 2, page_size=32),
            runtime_format=FORMATS[0],
        )
    with pytest.raises(ValueError, match="DP rank"):
        plan_kv_cache_store_load(
            layout, runtime, runtime_format=FORMATS[0], target_dp_rank=2
        )


@pytest.mark.parametrize("fmt", FORMATS)
def test_shared_record_round_trips(fmt):
    layout = _layout(fmt=fmt)
    manifest = _manifest(layout)
    assert (
        kv_cache_store_layout_from_json(kv_cache_store_layout_to_json(layout)) == layout
    )
    assert (
        kv_cache_store_manifest_from_json(kv_cache_store_manifest_to_json(manifest))
        == manifest
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("layout_digest", "0" * 64),
        ("layout_id", "sha256:" + "0" * 64),
        ("object_format", "unknown"),
        ("shards", []),
        ("snapshot", {}),
        ("token_count", 7),
    ],
)
def test_layout_wire_rejects_tampering(field, value):
    wire = json.loads(kv_cache_store_layout_to_json(_layout()))
    wire[field] = value
    with pytest.raises(ValueError):
        kv_cache_store_layout_from_json(json.dumps(wire))


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_domain", "0" * 64),
        ("manifest_digest", "0" * 64),
        ("model_revision", "wrong"),
        ("snapshot", {}),
        ("model_id", ""),
    ],
)
def test_manifest_wire_rejects_tampering(field, value):
    wire = json.loads(kv_cache_store_manifest_to_json(_manifest()))
    wire[field] = value
    with pytest.raises(ValueError):
        kv_cache_store_manifest_from_json(json.dumps(wire))


@pytest.mark.parametrize("wire", ["[]", "null", "{", '{"x":"a","x":"b"}', '{"x":NaN}'])
def test_hostile_json(wire):
    for decode in (kv_cache_store_layout_from_json, kv_cache_store_manifest_from_json):
        with pytest.raises(ValueError):
            decode(wire)


def test_intersection_expansion_obeys_range_limit():
    first = _placement("first", ((0,),), 2, total_kv_heads=12)
    target = _placement("target", ((0,),), 3, total_kv_heads=12)
    layout = plan_kv_cache_store_upload(first, object_format=FORMATS[0]).layout
    with pytest.raises(ValueError, match="transfer range limit"):
        plan_kv_cache_store_load(
            layout,
            target,
            runtime_format=FORMATS[0],
            limits=KVCacheStorePlanningLimits(max_fragments=6),
        )


def test_wire_aggregate_fragment_limit():
    wire = json.loads(kv_cache_store_layout_to_json(_layout()))
    wire["shards"] = [
        {
            "layer_ids": list(range(50001)),
            "head_start": 0,
            "head_count": 4,
            "key_suffix": "0",
        }
    ]
    with pytest.raises(ValueError, match="fragment count limit"):
        kv_cache_store_layout_from_json(json.dumps(wire))


@pytest.mark.parametrize("tp,pp", [(1, 3), (2, 1), (2, 2), (8, 2)])
def test_page_keys_reuse_hicache_prefix_and_stored_rank_suffix(tp, pp):
    partitions = tuple(tuple(range(i * 2, i * 2 + 2)) for i in range(pp))
    source = _placement("publisher", partitions, tp)
    layout = plan_kv_cache_store_upload(source, object_format=FORMATS[0]).layout
    manifest = _manifest(layout)
    page_key = "backend-model-revision_model_pagehash"
    expected = set()
    for part in source.parts:
        if part.replica_ordinal == 0:
            suffix = f"{part.rank.tp}_{part.rank.pp}" if pp > 1 else str(part.rank.tp)
            expected.update(
                f"{page_key}_l{layout.digest}_{suffix}_{c}" for c in ("k", "v")
            )
    assert set(manifest.object_keys(page_key)) == expected
    assert len(manifest.object_keys(page_key)) == len(expected)


def test_serialized_rank_parts_aggregate_before_building_shared_manifest():
    source = _placement("publisher", ((0, 1), (2, 3)), 2)
    wire_parts = [kv_cache_part_to_json(p) for p in reversed(source.parts)]
    assembled = assemble_kv_cache_placement(
        [kv_cache_part_from_json(w) for w in wire_parts],
        dp_size=1,
        pp_size=2,
        tp_size=2,
    )
    a = plan_kv_cache_store_upload(source, object_format=FORMATS[0])
    b = plan_kv_cache_store_upload(assembled, object_format=FORMATS[0])
    assert a.layout == b.layout and a.object_writers == b.object_writers
    with pytest.raises(ValueError):
        assemble_kv_cache_placement(
            [kv_cache_part_from_json(w) for w in wire_parts[:-1]],
            dp_size=1,
            pp_size=2,
            tp_size=2,
        )


def test_duplicate_shard_key_suffix_is_rejected():
    layout = _layout()
    with pytest.raises(ValueError, match="suffixes must be unique"):
        replace(
            layout, shards=tuple(replace(s, key_suffix="same") for s in layout.shards)
        )


@pytest.mark.parametrize("tp,pp", [(1, 3), (2, 2), (8, 2)])
def test_upload_object_has_one_complete_owner(tp, pp):
    source = _placement(
        "s", tuple(tuple(range(i * 2, i * 2 + 2)) for i in range(pp)), tp, dp_size=2
    )
    plan = plan_kv_cache_store_upload(
        source, object_format=FORMATS[0], source_dp_rank=1
    )
    assert isinstance(plan, KVCacheStoreUploadPlan)
    assert len(plan.object_writers) == len(plan.layout.object_sizes)
    for index, writer in enumerate(plan.object_writers):
        part = source.part(writer)
        shard = plan.layout.shards[index // 2]
        assert part.rank.dp == 1 and part.replica_ordinal == 0
        assert (part.layer_ids, part.head_start, part.head_count) == (
            shard.layer_ids,
            shard.head_start,
            shard.head_count,
        )
