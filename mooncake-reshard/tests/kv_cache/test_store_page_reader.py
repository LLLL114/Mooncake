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


def compiled_reader(native, reader, *, staging=False, target_ordered=False):
    native.prepare_get_into_ranges_template = lambda dst, src, sizes: (dst, src, sizes)
    native.prepare_get_into_ranges_snapshot = lambda keys: tuple(keys)
    native.get_into_ranges_from_snapshot = lambda snapshot, *args: (
        native.get_into_ranges(*args)
    )

    if staging:
        native.supports_ranged_read_staging = True

    def read(snapshot, templates, buffers, indices, keys, deltas, **kwargs):
        expected_options = {}
        if staging:
            expected_options["allow_staging"] = True
        assert kwargs == expected_options
        complete = []
        for template, index, key, delta in zip(templates, indices, keys, deltas):
            assert key in snapshot
            dst, src, sizes = template
            result = native.get_into_ranges(
                [buffers[index]],
                [[key]],
                [[[d + delta for d in dst]]],
                [[list(src)]],
                [[list(sizes)]],
            )
            complete.append(result == [[list(sizes)]])
        return complete

    native.get_into_ranges_from_template = read
    return reader.store.prepare_page_reader(
        reader.placement, reader.binding, target_ordered=target_ordered
    )


def test_compiled_reader_checks_exact_completion_and_api_pair():
    native, reader, context, offsets, keep = prepared()
    reader = compiled_reader(native, reader)
    assert reader.load(context, offsets) == 1
    assert [bytes(buf) for buf in keep[1]] == keep[2]
    native.short_key = reader.store.manifest.object_keys("page")[0]
    assert reader.load(context, offsets) == 0
    native.get_into_ranges_from_template = lambda *args: [1]
    with pytest.raises(KVCacheStoreError, match="shape"):
        reader.load(context, offsets)
    del native.get_into_ranges_from_template
    with pytest.raises(KVCacheStoreError, match="both"):
        reader.store.prepare_page_reader(reader.placement, reader.binding)


def test_compiled_reader_cancellation_drains_submitted_batch():
    native, reader, context, offsets, keep = prepared(
        KVCacheTransferLimits(max_batch_bytes=64)
    )
    reader = compiled_reader(native, reader)
    entered, release, cancelled = Event(), Event(), Event()
    original = native.get_into_ranges_from_template

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    native.get_into_ranges_from_template = blocked
    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(reader.load, context, offsets, cancelled=cancelled.is_set)
        assert entered.wait(5)
        cancelled.set()
        assert not result.done()
        release.set()
        assert result.result(timeout=5) == 0
    assert native.reads == 1 and keep


def test_large_configured_batches_keep_original_interface():
    native, reader, context, offsets, keep = prepared(
        KVCacheTransferLimits(max_batch_operations=100_001)
    )
    reader = compiled_reader(native, reader)

    def unexpected(*args):
        raise AssertionError("large configured batches used bounded template API")

    native.get_into_ranges_from_template = unexpected
    assert reader.load(context, offsets) == 1
    assert [bytes(buf) for buf in keep[1]] == keep[2]


@pytest.mark.parametrize("staging", [False, True])
def test_kv_staging_is_explicit_and_older_bindings_keep_their_signature(staging):
    native, reader, context, offsets, keep = prepared()
    reader = compiled_reader(native, reader, staging=staging)
    assert reader.load(context, offsets) == 1
    assert [bytes(buf) for buf in keep[1]] == keep[2]


@pytest.mark.parametrize("staging", [False, True])
def test_target_ordered_reader_is_opt_in_and_preserves_staging_choice(staging):
    native, reader, context, offsets, keep = prepared()
    reader = compiled_reader(native, reader, staging=staging, target_ordered=True)
    assert reader.load(context, offsets) == 1
    assert [bytes(buf) for buf in keep[1]] == keep[2]
    native.short_key = reader.store.manifest.object_keys("page")[0]
    assert reader.load(context, offsets) == 0


@pytest.mark.parametrize("source_format", list(KVCacheStoreFormat))
@pytest.mark.parametrize("target_format", list(KVCacheStoreFormat))
@pytest.mark.parametrize("compiled", [False, True])
def test_destination_runs_bind_reordered_pages_before_batching(
    source_format, target_format, compiled
):
    import ctypes

    from mooncake.reshard.kv_cache import KVCacheComponent
    from test_store_contracts import _reference

    source = _placement("source", ((0,), (1, 2), (3,)), 1)
    target = _placement("target", ((0, 1), (2, 3)), 2)
    native = MemoryNative()
    manifest = _manifest(
        plan_kv_cache_store_upload(source, object_format=source_format).layout
    )
    producer = KVCacheStore(MemoryBackend(native), manifest)
    producer.register_layout()
    pages = ("page-a", "page-b", "page-c")
    payloads = [
        bytes(_reference(manifest.layout, s.layer_ids, s.head_start, s.head_count, c))
        for s in manifest.layout.shards
        for c in KVCacheComponent
    ]
    for salt, page in enumerate(pages):
        for key, data in zip(manifest.object_keys(page), payloads):
            native.values[key] = bytes((v + salt) % 256 for v in data)
    consumer = KVCacheStore(
        MemoryBackend(native),
        _manifest(
            plan_kv_cache_store_upload(target, object_format=target_format).layout
        ),
        transfer_limits=KVCacheTransferLimits(
            max_batch_operations=5, max_batch_bytes=128
        ),
    )
    binding, _, expected_page = memory_binding(
        target, target.parts[0].participant_id, target_format, 1, "read", fill=False
    )
    buffers = [ctypes.create_string_buffer(r.nbytes * 5) for r in binding.regions]
    regions = tuple(
        replace(r, address=ctypes.addressof(b), nbytes=len(b))
        for r, b in zip(binding.regions, buffers)
    )
    offsets = [
        {
            r.region_id: slot * original.nbytes
            for r, original in zip(regions, binding.regions)
        }
        for slot in (2, 0, 4)
    ]
    reader = consumer.prepare_page_reader(
        target, replace(binding, regions=regions), target_ordered=True
    )
    if compiled:
        reader = compiled_reader(native, reader, target_ordered=True)
    visits = []
    original_read = native.get_into_ranges

    def capture(bases, keys, dst, src, sizes):
        for base, groups, lengths in zip(bases, dst, sizes):
            visits.extend(
                (base, d, n)
                for offsets_, sizes_ in zip(groups, lengths)
                for d, n in zip(offsets_, sizes_)
            )
        return original_read(bases, keys, dst, src, sizes)

    native.get_into_ranges = capture
    context = consumer.discover(pages, operation_id="read")
    assert reader.load(context, offsets) == len(pages)
    assert [(b, d) for b, d, _ in visits] == sorted((b, d) for b, d, _ in visits)
    assert native.reads > 1
    for buffer, expected in zip(buffers, expected_page):
        answer = bytearray(len(buffer))
        for salt, slot in enumerate((2, 0, 4)):
            answer[slot * len(expected) : (slot + 1) * len(expected)] = bytes(
                (v + salt) % 256 for v in expected
            )
        assert bytes(buffer) == answer
    # Completion is still in caller page order, not physical destination order.
    native.short_key = manifest.object_keys(pages[1])[0]
    assert reader.load(context, offsets) == 1
