"""Every physical axis permutation restores logical KV without changing semantics."""

import json
from dataclasses import asdict, replace

import pytest
from mooncake.reshard.kv_cache import (
    KVCacheStore,
    KVCacheStoreFormat,
    KVCacheTransferLimits,
    kv_cache_store_manifest_from_json,
    kv_cache_store_manifest_to_json,
    plan_kv_cache_store_upload,
)
from mooncake.reshard.kv_cache.snapshot import _canonical_digest
from test_kv_cache_reshard import _placement
from test_store_contracts import _manifest
from test_store_execution import MemoryBackend, MemoryNative, memory_binding

FORMATS = tuple(KVCacheStoreFormat)


@pytest.mark.parametrize("source_format", FORMATS)
@pytest.mark.parametrize("target_format", FORMATS)
@pytest.mark.parametrize("source_tp", [1, 2, 8])
@pytest.mark.parametrize("prepared", [False, True])
def test_cross_layout_restores_exact_bytes(
    source_format, target_format, source_tp, prepared
):
    native = MemoryNative()
    source = _placement("source", ((0,), (1, 2), (3,)), source_tp)
    target = _placement("target", ((0, 1), (2, 3)), 2)
    plan = plan_kv_cache_store_upload(source, object_format=source_format)
    writer = KVCacheStore(MemoryBackend(native), _manifest(plan.layout))
    writer.register_layout()
    for part in source.parts:
        binding, keep, _ = memory_binding(
            source, part.participant_id, source_format, 1, "put"
        )
        assert writer.upload(plan, ("page",), binding, operation_id="put") == (True,)
        assert keep
    reader = KVCacheStore(
        MemoryBackend(native),
        _manifest(
            plan_kv_cache_store_upload(target, object_format=target_format).layout
        ),
        transfer_limits=KVCacheTransferLimits(
            max_batch_operations=7, max_batch_bytes=173
        ),
    )
    reader.register_layout()
    context = reader.discover(("page", "missing"), operation_id="get")
    assert context.page_keys == ("page",)
    assert context.manifests[0].layout.object_format == source_format
    for part in target.parts:
        binding, buffers, expected = memory_binding(
            target, part.participant_id, target_format, 1, "get", fill=False
        )
        page_reader = reader.prepare_page_reader(target, binding) if prepared else None
        load = (
            (
                lambda: page_reader.load(
                    context, [{r.region_id: 0 for r in binding.regions}]
                )
            )
            if prepared
            else (lambda: reader.load(context, target, binding))
        )
        assert load() == 1
        assert [bytes(b) for b in buffers] == expected
        native.short_key = writer.manifest.object_keys("page")[0]
        # Only ranks that consume this source object should observe the short read.
        if part.rank.tp == 0 and part.rank.pp == 0:
            assert load() == 0
        native.short_key = None


def test_mixed_layout_pages_share_catalog_but_keep_distinct_objects():
    native = MemoryNative()
    placement = _placement("source", ((0, 1), (2, 3)), 2)
    pages = tuple("page-" + fmt.value for fmt in FORMATS)
    for fmt, page in zip(FORMATS, pages):
        plan = plan_kv_cache_store_upload(placement, object_format=fmt)
        writer = KVCacheStore(MemoryBackend(native), _manifest(plan.layout))
        writer.register_layout()
        for part in placement.parts:
            binding, keep, _ = memory_binding(
                placement, part.participant_id, fmt, 1, "put", page_salts=(0,)
            )
            assert writer.upload(plan, (page,), binding, operation_id="put") == (True,)
            assert keep
    for fmt in FORMATS:
        reader = KVCacheStore(
            MemoryBackend(native),
            _manifest(plan_kv_cache_store_upload(placement, object_format=fmt).layout),
        )
        context = reader.discover(pages, operation_id="get")
        assert len(set(context.layout_ids)) == 3
        for part in placement.parts:
            binding, buffers, expected = memory_binding(
                placement,
                part.participant_id,
                fmt,
                3,
                "get",
                fill=False,
                page_salts=(0, 0, 0),
            )
            assert reader.load(context, placement, binding) == 3
            assert [bytes(b) for b in buffers] == expected


def test_format_independent_domain_preserves_plhd_keys_and_semantic_isolation():
    manifest = _manifest()
    legacy_plhd_domain = _canonical_digest(
        {
            "namespace": manifest.namespace,
            "model_id": manifest.model_id,
            "model_revision": manifest.model_revision,
            "semantic_fingerprint": manifest.semantic_fingerprint,
            "descriptor": asdict(manifest.layout.descriptor),
            "object_format": "PLHD",
        }
    )
    assert manifest.model_domain == legacy_plhd_domain
    for fmt in FORMATS:
        other = replace(manifest, layout=replace(manifest.layout, object_format=fmt))
        assert other.model_domain == manifest.model_domain
        assert (
            kv_cache_store_manifest_from_json(kv_cache_store_manifest_to_json(other))
            == other
        )
        if fmt != KVCacheStoreFormat.PLHD:
            assert set(other.object_keys("page")).isdisjoint(
                manifest.object_keys("page")
            )
        for field in ("namespace", "model_revision", "semantic_fingerprint"):
            assert (
                replace(other, **{field: "changed"}).model_domain
                != manifest.model_domain
            )
    for field, value in [("page_size", 32), ("dtype", "bfloat16")]:
        other = replace(
            manifest,
            layout=replace(
                manifest.layout,
                descriptor=replace(manifest.layout.descriptor, **{field: value}),
            ),
        )
        assert other.model_domain != manifest.model_domain


def test_old_format_specific_domains_fail_closed():
    for fmt in (KVCacheStoreFormat.LPHD, KVCacheStoreFormat.HPLD):
        manifest = _manifest()
        manifest = replace(manifest, layout=replace(manifest.layout, object_format=fmt))
        payload = json.loads(kv_cache_store_manifest_to_json(manifest))
        payload["model_domain"] = _canonical_digest(
            {
                "namespace": manifest.namespace,
                "model_id": manifest.model_id,
                "model_revision": manifest.model_revision,
                "semantic_fingerprint": manifest.semantic_fingerprint,
                "descriptor": asdict(manifest.layout.descriptor),
                "object_format": fmt,
            }
        )
        with pytest.raises(ValueError, match="model domain"):
            kv_cache_store_manifest_from_json(json.dumps(payload))
