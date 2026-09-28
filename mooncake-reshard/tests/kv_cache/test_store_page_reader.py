"""Prepared page reads preserve validation, cancellation and short-read rules."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from mooncake.reshard.kv_cache import (
    KVCacheStore,
    KVCacheStoreError,
    KVCacheStoreFormat,
    KVCacheTransferLimits,
    plan_kv_cache_store_upload,
)
from test_kv_cache_reshard import _placement
from test_store_contracts import _manifest
from test_store_execution import MemoryBackend, MemoryNative, memory_binding


def prepared(limits=None):
    limits = limits or KVCacheTransferLimits()
    native = MemoryNative()
    placement = _placement("s", ((0, 1),), 1)
    plan = plan_kv_cache_store_upload(placement, object_format=KVCacheStoreFormat.PLHD)
    store = KVCacheStore(MemoryBackend(native), _manifest(plan.layout))
    store.register_layout()
    source, keep_source, _ = memory_binding(
        placement, placement.parts[0].participant_id, KVCacheStoreFormat.PLHD, 1, "put"
    )
    assert store.upload(plan, ("page",), source, operation_id="put") == (True,)
    context = store.discover(("page",), operation_id="get")
    target, keep_target, expected = memory_binding(
        placement,
        placement.parts[0].participant_id,
        KVCacheStoreFormat.PLHD,
        1,
        "template",
        fill=False,
    )
    store.transfer_limits = limits
    reader = store.prepare_page_reader(placement, target)
    return (
        native,
        reader,
        context,
        [{key: 0 for key in reader.regions}],
        (keep_source, keep_target, expected),
    )


def test_small_native_batches_copy_exact_bytes_and_short_reads_fail():
    native, reader, context, offsets, keep = prepared(
        KVCacheTransferLimits(max_batch_operations=2, max_batch_bytes=64)
    )
    assert reader.load(context, offsets) == 1
    assert native.reads > 1
    assert [bytes(buf) for buf in keep[1]] == keep[2]
    native.short_key = reader.store.manifest.object_keys("page")[0]
    assert reader.load(context, offsets) == 0


def test_prepared_reader_cancellation_waits_for_submitted_io():
    native, reader, context, offsets, keep = prepared(
        KVCacheTransferLimits(max_batch_bytes=64)
    )
    entered, release, cancelled = Event(), Event(), Event()
    original = native.get_into_ranges

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    native.get_into_ranges = blocked
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(reader.load, context, offsets, cancelled=cancelled.is_set)
        assert entered.wait(5)
        cancelled.set()
        assert not future.done()
        release.set()
        assert future.result(timeout=5) == 0
    assert native.reads == 1
    assert keep


def test_prepared_reader_rejects_bad_translations_and_snapshot_binding():
    native, reader, context, _offsets, keep = prepared()
    with pytest.raises(ValueError, match="translations"):
        reader.load(context, [])
    with pytest.raises(ValueError, match="regions"):
        reader.load(context, [{}])
    for delta in (-1, True, 1, 1 << 62):
        with pytest.raises(ValueError):
            reader.load(context, [{key: delta for key in reader.regions}])
    with pytest.raises(ValueError, match="snapshot"):
        reader.store.prepare_page_reader(
            reader.placement,
            replace(reader.binding, snapshot_id="s", snapshot_digest="0" * 64),
        )
    assert native.reads == 0 and keep


def test_prepared_reader_rejects_bad_native_shape():
    native, reader, context, offsets, keep = prepared()
    native.get_into_ranges = lambda *args: []
    with pytest.raises(KVCacheStoreError, match="shape"):
        reader.load(context, offsets)
    assert keep
