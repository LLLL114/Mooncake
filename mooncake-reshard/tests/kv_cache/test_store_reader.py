"""Reshard exact bytes, mixed source layouts, reuse and invalid destinations."""

from dataclasses import replace

import pytest
from _store_test_utils import (
    FORMATS,
    MemoryNative,
    _manifest,
    memory_binding,
)
from mooncake.reshard.kv_cache import (
    KVCacheStoreError,
    KVCacheStoreFormat,
    KVCacheTransferLimits,
    MultipartKVCacheStore,
    plan_kv_cache_store_upload,
)
from mooncake.reshard.kv_cache._store import multipart
from mooncake.reshard.kv_cache._store.backend import manifest_metadata
from test_kv_cache_reshard import _placement


def setup_page():
    native = MemoryNative()
    source = _placement("source", ((0, 1), (2, 3)), 1)
    target = _placement("target", ((0, 1, 2, 3),), 2)
    fmt = KVCacheStoreFormat.PLHD
    plan = plan_kv_cache_store_upload(source, object_format=fmt)
    writer = MultipartKVCacheStore(native, _manifest(plan.layout))
    for part in source.parts:
        binding, keep, _ = memory_binding(source, part.participant_id, fmt, 1, "put")
        assert writer.upload(plan, ("page",), binding, operation_id="put") == (True,)
        assert keep
    store = MultipartKVCacheStore(
        native, _manifest(plan_kv_cache_store_upload(target, object_format=fmt).layout)
    )
    binding, buffers, expected = memory_binding(
        target, target.parts[0].participant_id, fmt, 1, "get", fill=False
    )
    return native, store, target, binding, buffers, expected


@pytest.mark.parametrize(
    "source_format,target_format,source_tp",
    [
        (KVCacheStoreFormat.PLHD, KVCacheStoreFormat.LPHD, 1),
        (KVCacheStoreFormat.LPHD, KVCacheStoreFormat.HPLD, 2),
        (KVCacheStoreFormat.HPLD, KVCacheStoreFormat.PLHD, 8),
        (KVCacheStoreFormat.PLHD, KVCacheStoreFormat.HPLD, 8),
        (KVCacheStoreFormat.HPLD, KVCacheStoreFormat.LPHD, 1),
        (KVCacheStoreFormat.LPHD, KVCacheStoreFormat.PLHD, 2),
    ],
)
def test_cross_layout_restores_exact_bytes(source_format, target_format, source_tp):
    native = MemoryNative()
    source = _placement("source", ((0,), (1, 2), (3,)), source_tp)
    target = _placement("target", ((0, 1), (2, 3)), 2)
    plan = plan_kv_cache_store_upload(source, object_format=source_format)
    writer = MultipartKVCacheStore(native, _manifest(plan.layout))
    for part in source.parts:
        if part.participant_id not in plan.part_writers:
            continue
        binding, keep, _ = memory_binding(
            source, part.participant_id, source_format, 1, "put"
        )
        assert writer.upload(plan, ("page",), binding, operation_id="put") == (True,)
        assert keep
    reader = MultipartKVCacheStore(
        native,
        _manifest(
            plan_kv_cache_store_upload(target, object_format=target_format).layout
        ),
    )
    reader.transfer_limits = KVCacheTransferLimits(
        max_batch_operations=7, max_batch_bytes=173
    )
    context = reader.discover(("page", "missing"), operation_id="get")
    assert context.page_keys == ("page",)
    assert context.manifests[0].layout.object_format == source_format
    for part in target.parts:
        binding, buffers, expected = memory_binding(
            target, part.participant_id, target_format, 1, "get", fill=False
        )
        page_reader = reader.prepare_page_reader(target, binding)
        assert (
            page_reader.load(context, [{r.region_id: 0 for r in binding.regions}]) == 1
        )
        assert [bytes(b) for b in buffers] == expected


def test_mixed_layout_pages_bind_independent_inline_manifests():
    native = MemoryNative()
    placement = _placement("source", ((0, 1), (2, 3)), 2)
    pages = tuple("page-" + fmt.value for fmt in FORMATS)
    for fmt, page in zip(FORMATS, pages):
        plan = plan_kv_cache_store_upload(placement, object_format=fmt)
        writer = MultipartKVCacheStore(native, _manifest(plan.layout))
        for part in placement.parts:
            binding, keep, _ = memory_binding(
                placement, part.participant_id, fmt, 1, "put", page_salts=(0,)
            )
            assert writer.upload(plan, (page,), binding, operation_id="put") == (True,)
            assert keep
    for fmt in FORMATS:
        reader = MultipartKVCacheStore(
            native,
            _manifest(plan_kv_cache_store_upload(placement, object_format=fmt).layout),
        )
        context = reader.discover(pages, operation_id="get")
        assert len(set(context.layout_ids)) == 3
        for part in placement.parts:
            binding, buffers, expected = memory_binding(
                placement,
                part.participant_id,
                fmt,
                1,
                "get",
                fill=False,
                page_salts=(0,),
            )
            page_reader = reader.prepare_page_reader(placement, binding)
            for page, layout_id in zip(context.page_keys, context.layout_ids):
                selected = replace(
                    context,
                    page_keys=(page,),
                    layout_ids=(layout_id,),
                    manifests=tuple(
                        m for m in context.manifests if m.layout.layout_id == layout_id
                    ),
                )
                assert (
                    page_reader.load(
                        selected, [{r.region_id: 0 for r in binding.regions}]
                    )
                    == 1
                )
                assert [bytes(b) for b in buffers] == expected
            assert len(page_reader._templates) == 3


def test_manifest_and_geometry_cache_are_local_and_immutable(monkeypatch):
    _, store, target, binding, buffers, expected = setup_page()
    context = store.discover(("page", "missing"), operation_id="get")
    assert context.page_keys == ("page",)
    assert tuple(store._manifests) == (context.manifests[0].manifest_key,)
    reader = store.prepare_page_reader(target, binding)
    offsets = [{key: 0 for key in reader.regions}]
    assert reader.load(context, offsets) == 1
    assert [bytes(b) for b in buffers] == expected

    def forbidden(*args):
        raise AssertionError("cached manifest/template should not be rebuilt")

    monkeypatch.setattr(reader, "_compile", forbidden)
    monkeypatch.setattr(multipart, "kv_cache_store_manifest_from_json", forbidden)
    assert reader.load(store.discover(("page",), operation_id="again"), offsets) == 1


def test_reader_honors_range_limits_and_cancellation():
    native, store, target, binding, buffers, expected = setup_page()
    store.transfer_limits = KVCacheTransferLimits(
        max_batch_operations=2, max_batch_bytes=137
    )
    context = store.discover(("page",), operation_id="get")
    reader = store.prepare_page_reader(target, binding)
    offsets = [{key: 0 for key in reader.regions}]
    assert reader.load(context, offsets, cancelled=lambda: True) == 0
    assert native.range_reads == 0
    assert reader.load(context, offsets) == 1
    assert native.range_reads > 1
    assert [bytes(b) for b in buffers] == expected
    with pytest.raises(ValueError, match="regions"):
        reader.load(context, [{}])
    with pytest.raises(ValueError, match="bounds"):
        reader.load(context, [{key: 1 << 40 for key in reader.regions}])
    native.get_into_ranges_from_template = lambda *a, **kw: []
    with pytest.raises(KVCacheStoreError, match="shape"):
        reader.load(context, offsets)


def test_manifest_digest_is_checked_before_planning():
    native, store, _, _, _, _ = setup_page()
    metadata, parts = native.parts["page"]
    native.parts["page"] = (
        metadata.replace('"model_id":"model"', '"model_id":"wrong"'),
        parts,
    )
    with pytest.raises(ValueError, match="domain"):
        store.discover(("page",), operation_id="get")


@pytest.mark.parametrize(
    "metadata", ["reference-only", "\n{}", "key\n", "wrong-key\n{}"]
)
def test_discovery_rejects_missing_or_invalid_inline_manifest(metadata):
    native, store, _, _, _, _ = setup_page()
    native.parts["page"] = metadata, native.parts["page"][1]
    with pytest.raises((KVCacheStoreError, ValueError)):
        store.discover(("page",), operation_id="get")
    assert not store._manifests


def test_inline_manifest_identity_and_part_count_are_checked():
    native, store, _, _, _, _ = setup_page()
    metadata, parts = native.parts["page"]
    _, body = metadata.split("\n", 1)
    native.parts["page"] = "wrong-key\n" + body, parts
    with pytest.raises(KVCacheStoreError, match="identity"):
        store.discover(("page",), operation_id="wrong-key")
    assert not store._manifests
    native.parts["page"] = metadata, parts[:-1]
    with pytest.raises(KVCacheStoreError, match="part count"):
        store.discover(("page",), operation_id="wrong-count")
    assert not store._manifests
    native.parts["page"] = metadata, parts
    store.discover(("page",), operation_id="valid")
    native.parts["page"] = metadata, parts[:-1]
    with pytest.raises(KVCacheStoreError, match="cached manifest part count"):
        store.discover(("page",), operation_id="cached-wrong-count")


def test_evicted_manifest_is_reparsed_without_reading_a_separate_object(monkeypatch):
    native, store, _, _, _, _ = setup_page()
    store.cache_capacity = 1
    other = store.manifest
    native.parts["other"] = manifest_metadata(other), [b""] * len(other.layout.shards)
    parse = multipart.kv_cache_store_manifest_from_json
    parsed = []

    def tracked(value):
        manifest = parse(value)
        parsed.append(manifest.manifest_key)
        return manifest

    monkeypatch.setattr(multipart, "kv_cache_store_manifest_from_json", tracked)
    for page in ("page", "page", "other", "other", "page"):
        store.discover((page,), operation_id="get")
    first = native.parts["page"][0].split("\n", 1)[0]
    assert parsed == [first, other.manifest_key, first]
    assert tuple(store._manifests) == (first,)
    # This backend intentionally has no standalone object put/get methods.
    assert not hasattr(native, "put") and not hasattr(native, "get")


def test_read_snapshot_rejects_metadata_changed_after_discovery(monkeypatch):
    native, store, target, binding, buffers, _ = setup_page()
    context = store.discover(("page",), operation_id="get")
    reader = store.prepare_page_reader(target, binding)
    metadata, parts = native.parts["page"]
    native.parts["page"] = metadata + " ", parts

    def forbidden(*args):
        raise AssertionError("cache hit must not parse JSON")

    monkeypatch.setattr(multipart, "kv_cache_store_manifest_from_json", forbidden)
    cached = store.discover(("page",), operation_id="cached")
    assert cached.manifests == context.manifests
    assert reader.load(cached, [{key: 0 for key in reader.regions}]) == 0
    assert all(not any(bytes(buffer)) for buffer in buffers)
