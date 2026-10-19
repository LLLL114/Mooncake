"""Reshard exact bytes, mixed source layouts, reuse and invalid destinations."""

from dataclasses import replace

import pytest
from _store_test_utils import (
    FORMATS,
    MemoryBackend,
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
from mooncake.reshard.kv_cache._store.backend import MultipartBackend
from test_kv_cache_reshard import _placement


@pytest.fixture(autouse=True)
def metadata_config(monkeypatch):
    monkeypatch.setattr(MultipartBackend, "metadata_config", lambda self: None)


def setup_page():
    native = MemoryNative()
    source = _placement("source", ((0, 1), (2, 3)), 1)
    target = _placement("target", ((0, 1, 2, 3),), 2)
    fmt = KVCacheStoreFormat.PLHD
    plan = plan_kv_cache_store_upload(source, object_format=fmt)
    writer = MultipartKVCacheStore(native, _manifest(plan.layout))
    writer.register_layout()
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
    writer = MultipartKVCacheStore(MemoryBackend(native), _manifest(plan.layout))
    writer.register_layout()
    for part in source.parts:
        if part.participant_id not in plan.part_writers:
            continue
        binding, keep, _ = memory_binding(
            source, part.participant_id, source_format, 1, "put"
        )
        assert writer.upload(plan, ("page",), binding, operation_id="put") == (True,)
        assert keep
    reader = MultipartKVCacheStore(
        MemoryBackend(native),
        _manifest(
            plan_kv_cache_store_upload(target, object_format=target_format).layout
        ),
    )
    reader.transfer_limits = KVCacheTransferLimits(
        max_batch_operations=7, max_batch_bytes=173
    )
    reader.register_layout()
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


def test_mixed_layout_pages_bind_independent_manifest_references():
    native = MemoryNative()
    placement = _placement("source", ((0, 1), (2, 3)), 2)
    pages = tuple("page-" + fmt.value for fmt in FORMATS)
    for fmt, page in zip(FORMATS, pages):
        plan = plan_kv_cache_store_upload(placement, object_format=fmt)
        writer = MultipartKVCacheStore(MemoryBackend(native), _manifest(plan.layout))
        writer.register_layout()
        for part in placement.parts:
            binding, keep, _ = memory_binding(
                placement, part.participant_id, fmt, 1, "put", page_salts=(0,)
            )
            assert writer.upload(plan, (page,), binding, operation_id="put") == (True,)
            assert keep
    for fmt in FORMATS:
        reader = MultipartKVCacheStore(
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
    native, store, target, binding, buffers, expected = setup_page()
    context = store.discover(("page", "missing"), operation_id="get")
    assert context.page_keys == ("page",)
    assert len(store._manifests) == 1
    reader = store.prepare_page_reader(target, binding)
    offsets = [{key: 0 for key in reader.regions}]
    assert reader.load(context, offsets) == 1
    assert [bytes(b) for b in buffers] == expected
    reads = native.manifest_reads

    def forbidden(*args):
        raise AssertionError("cached manifest/template should not be rebuilt")

    monkeypatch.setattr(reader, "_compile", forbidden)
    assert reader.load(store.discover(("page",), operation_id="again"), offsets) == 1
    assert native.manifest_reads == reads


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
    ref, _ = native.parts["page"]
    data = native.values[ref].decode()
    native.values[ref] = data.replace(
        '"model_id":"model"', '"model_id":"wrong"'
    ).encode()
    with pytest.raises(ValueError, match="domain"):
        store.discover(("page",), operation_id="get")
