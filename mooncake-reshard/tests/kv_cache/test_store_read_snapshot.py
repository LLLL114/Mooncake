"""A load reuses metadata across batches without caching it across KV restores."""

import ctypes
from dataclasses import replace

import pytest
from mooncake.reshard.kv_cache import KVCacheStoreError, KVCacheTransferLimits
from test_store_page_reader import prepared


@pytest.fixture(params=["load", "page_reader"])
def ranged_load(request):
    native, reader, context, offsets, keep = prepared(
        KVCacheTransferLimits(max_batch_operations=2, max_batch_bytes=64)
    )
    if request.param == "load":
        binding = replace(reader.binding, operation_id=context.operation_id)

        def load(**kwargs):
            return reader.store.load(context, reader.placement, binding, **kwargs)

    else:

        def load(**kwargs):
            return reader.load(context, offsets, **kwargs)

    return native, reader, keep, load


def snapshot_api(native):
    prepared_keys, snapshots, reads = [], [], []
    plain_read = native.get_into_ranges

    def prepare(keys):
        prepared_keys.append(keys)
        snapshot = object()
        snapshots.append(snapshot)
        return snapshot

    def read(snapshot, buffers, keys, dst, src, sizes):
        assert snapshot in snapshots
        assert all(
            key in prepared_keys[snapshots.index(snapshot)]
            for row in keys
            for key in row
        )
        reads.append(snapshot)
        return plain_read(buffers, keys, dst, src, sizes)

    def unexpected_plain_read(*args):
        raise AssertionError("snapshot load fell back to an ordinary metadata query")

    native.prepare_get_into_ranges_snapshot = prepare
    native.get_into_ranges_from_snapshot = read
    native.get_into_ranges = unexpected_plain_read
    return prepared_keys, snapshots, reads


def test_snapshot_reused_across_batches_and_recreated_for_each_load(ranged_load):
    native, reader, keep, load = ranged_load
    keys, snapshots, reads = snapshot_api(native)
    assert load() == 1
    assert [bytes(buf) for buf in keep[1]] == keep[2]
    assert len(snapshots) == 1 and len(reads) > 1
    assert all(snapshot is snapshots[0] for snapshot in reads)
    assert keys == [list(reader.store.manifest.object_keys("page"))]
    previous_reads = len(reads)
    assert load() == 1
    assert len(snapshots) == 2
    assert all(snapshot is snapshots[1] for snapshot in reads[previous_reads:])


def test_snapshot_reads_preserve_short_read_and_eviction_failures(ranged_load):
    native, reader, _keep, load = ranged_load
    snapshot_api(native)
    key = reader.store.manifest.object_keys("page")[0]
    native.short_key = key
    assert load() == 0
    native.short_key = None
    del native.values[key]
    assert load() == 0


def test_cancelled_load_does_not_prepare_or_submit_more_batches(ranged_load):
    native, _reader, _keep, load = ranged_load
    keys, snapshots, reads = snapshot_api(native)
    assert load(cancelled=lambda: True) == 0
    assert not keys and not snapshots and not reads
    assert load(cancelled=lambda: native.reads > 0) == 0
    assert len(snapshots) == 1 and len(reads) == 1


@pytest.mark.parametrize("missing", ["prepare", "read"])
def test_partial_snapshot_api_fails_before_io(ranged_load, missing):
    native, _reader, _keep, load = ranged_load
    snapshot_api(native)
    delattr(
        native,
        "prepare_get_into_ranges_snapshot"
        if missing == "prepare"
        else "get_into_ranges_from_snapshot",
    )
    with pytest.raises(KVCacheStoreError, match="both"):
        load()
    assert native.reads == 0


def test_invalid_snapshot_does_not_fall_back_to_plain_reads(ranged_load):
    native, _reader, _keep, load = ranged_load
    snapshot_api(native)
    native.prepare_get_into_ranges_snapshot = lambda keys: None
    with pytest.raises(KVCacheStoreError, match="invalid snapshot"):
        load()
    assert native.reads == 0


def test_whole_object_page_reads_do_not_prepare_unused_snapshot():
    native, reader, context, offsets, keep = prepared()
    keys, snapshots, reads = snapshot_api(native)
    whole_reads = []

    def whole(keys, pointers, sizes):
        whole_reads.append(keys)
        for key, destinations, lengths in zip(keys, pointers, sizes):
            offset = 0
            for pointer, size in zip(destinations, lengths):
                ctypes.memmove(
                    pointer, native.values[key][offset : offset + size], size
                )
                offset += size
        return [sum(row) for row in sizes]

    native.batch_get_into_multi_buffers = whole
    native.batch_get_into = lambda keys, pointers, sizes: whole(
        keys, [[p] for p in pointers], [[n] for n in sizes]
    )
    assert reader.load(context, offsets) == 1
    assert whole_reads
    assert not keys and not snapshots and not reads
    assert [bytes(buf) for buf in keep[1]] == keep[2]
