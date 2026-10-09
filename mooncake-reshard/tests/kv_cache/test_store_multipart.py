"""Part visibility, manifest caching, direct planning and failed-read isolation."""

import pytest
from mooncake.reshard.kv_cache import (
    KVCacheStoreError,
    KVCacheStoreFormat,
    KVCacheTransferLimits,
    MultipartKVCacheStore,
    plan_kv_cache_store_upload,
)
from mooncake.reshard.kv_cache._store.backend import MultipartBackend
from test_kv_cache_reshard import _placement
from test_store_contracts import _manifest
from test_store_execution import MemoryNative, memory_binding


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
    for i, part in enumerate(source.parts):
        binding, keep, _ = memory_binding(source, part.participant_id, fmt, 1, "put")
        assert writer.upload(plan, ("page",), binding, operation_id="put") == (True,)
        assert bool(writer.discover(("page",), operation_id="probe").page_keys) == (
            i == 1
        )
        assert keep
    store = MultipartKVCacheStore(
        native, _manifest(plan_kv_cache_store_upload(target, object_format=fmt).layout)
    )
    binding, buffers, expected = memory_binding(
        target, target.parts[0].participant_id, fmt, 1, "get", fill=False
    )
    return native, store, target, binding, buffers, expected


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
    assert not hasattr(native, "compare_exchange_metadata")


def test_source_reference_rechecked_and_missing_part_never_hits():
    native, store, target, binding, buffers, _ = setup_page()
    context = store.discover(("page",), operation_id="read")
    reader = store.prepare_page_reader(target, binding)
    offsets = [{key: 0 for key in reader.regions}]
    ref, parts = native.parts["page"]
    native.parts["page"] = ("a-different-manifest", parts)
    assert reader.load(context, offsets) == 0
    assert all(not any(bytes(b)) for b in buffers)
    native.parts["page"] = (ref, [parts[0], None])
    assert not store.discover(("page",), operation_id="missing").page_keys
    assert reader.load(context, offsets) == 0


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
