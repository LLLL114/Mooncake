"""Executable byte copies with framework-owned host buffers and fake Store I/O."""

from __future__ import annotations

import ctypes
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event, Lock

import pytest
from mooncake.reshard.kv_cache import (
    KVCacheComponent,
    KVCacheRegisteredRegion,
    KVCacheResolvedRange,
    KVCacheResolvedRuntimeBinding,
    KVCacheStore,
    KVCacheStoreError,
    KVCacheStoreFormat,
    KVCacheTransferLimits,
    kv_cache_resolved_binding_to_json,
    kv_cache_store_catalog_from_json,
    plan_kv_cache_store_upload,
    validate_resolved_runtime_binding,
)
from mooncake.reshard.kv_cache._store.backend import StoreBackend
from test_kv_cache_reshard import _placement, _snapshot
from test_store_contracts import _manifest, _reference


class MemoryNative:
    def __init__(self):
        self.values = {}
        self.tokens = {}
        self.lock = Lock()
        self.revision = 0
        self.barrier = None
        self.catalog_reads = 0
        self.manifest_batches = 0
        self.reads = 0
        self.writes = 0
        self.short_key = None
        self.lost_reply = False

    def read_metadata_for_update(self, key):
        with self.lock:
            self.catalog_reads += 1
            result = (
                (1, self.values[key], self.tokens[key])
                if key in self.values
                else (0, b"", "")
            )
            barrier = self.barrier if self.catalog_reads <= 2 else None
        if barrier:
            barrier.wait(timeout=10)
        return result

    def compare_exchange_metadata(self, key, token, value):
        with self.lock:
            if self.tokens.get(key, "") != token:
                return 0
            self.revision += 1
            self.tokens[key] = str(self.revision)
            self.values[key] = value
            if self.lost_reply:
                self.lost_reply = False
                return -500
            return 1

    def put(self, key, value, config):
        with self.lock:
            self.values.setdefault(key, value)
        return 0

    def get(self, key):
        return self.values.get(key, b"")

    def batch_get_buffer(self, keys):
        self.manifest_batches += 1
        return [self.values.get(key) for key in keys]

    def batch_is_exist(self, keys):
        return [int(key in self.values) for key in keys]

    def batch_put_from(self, keys, ptrs, sizes, config):
        return self.batch_put_from_multi_buffers(
            keys, [[p] for p in ptrs], [[n] for n in sizes], config
        )

    def batch_put_from_multi_buffers(self, keys, ptrs, sizes, config):
        self.writes += 1
        for key, addresses, lengths in zip(keys, ptrs, sizes):
            self.values.setdefault(
                key,
                b"".join(ctypes.string_at(p, n) for p, n in zip(addresses, lengths)),
            )
        return [0] * len(keys)

    def get_into_ranges(self, buffers, all_keys, dst, src, sizes):
        self.reads += 1
        result = []
        for i, (base, keys) in enumerate(zip(buffers, all_keys)):
            rows = []
            for j, key in enumerate(keys):
                values = []
                data = self.values.get(key)
                for d, s, size in zip(dst[i][j], src[i][j], sizes[i][j]):
                    if data is None or s + size > len(data):
                        values.append(-1)
                    else:
                        ctypes.memmove(base + d, data[s : s + size], size)
                        values.append(size - 1 if key == self.short_key else size)
                rows.append(values)
            result.append(rows)
        return result


class MemoryBackend(StoreBackend):
    def metadata_config(self):
        return None

    def payload_config(self):
        return None


def memory_binding(
    placement,
    participant,
    fmt,
    pages,
    operation,
    *,
    fill=True,
    endpoint="local",
    allocator=None,
    page_salts=None,
):
    part = placement.part(participant)
    d = placement.descriptor
    layout = plan_kv_cache_store_upload(placement, object_format=fmt).layout
    regions, ranges, buffers, expected = [], [], [], []
    for page in range(pages):
        for component in KVCacheComponent:
            data = _reference(
                layout, part.layer_ids, part.head_start, part.head_count, component
            )
            # Keep page contents distinguishable, while preserving width and shape.
            data = bytes(
                (b + (page if page_salts is None else page_salts[page])) % 256
                for b in data
            )
            if allocator is None:
                buffer = ctypes.create_string_buffer(
                    data if fill else bytes(len(data)), len(data)
                )
            else:
                address = allocator(len(data))
                assert address
                buffer = (ctypes.c_char * len(data)).from_address(address)
                ctypes.memmove(address, data if fill else bytes(len(data)), len(data))
            buffers.append(buffer)
            expected.append(data)
            region_id = f"page-{page}-{component.value}"
            regions.append(
                KVCacheRegisteredRegion(
                    region_id, endpoint, ctypes.addressof(buffer), len(data)
                )
            )
            dim = (
                d.key_head_dim
                if component is KVCacheComponent.KEY
                else d.value_head_dim
            )
            width = dim * d.itemsize
            layers, heads, tokens = len(part.layer_ids), part.head_count, d.page_size
            if fmt is KVCacheStoreFormat.PLHD:
                layer_stride, token_stride, head_stride = (
                    heads * width,
                    layers * heads * width,
                    width,
                )
            elif fmt is KVCacheStoreFormat.LPHD:
                layer_stride, token_stride, head_stride = (
                    tokens * heads * width,
                    heads * width,
                    width,
                )
            else:
                layer_stride, token_stride, head_stride = (
                    width,
                    layers * width,
                    tokens * layers * width,
                )
            for l, layer in enumerate(part.layer_ids):
                for h in range(heads) if fmt is KVCacheStoreFormat.HPLD else (0,):
                    ranges.append(
                        KVCacheResolvedRange(
                            layer,
                            component,
                            page * tokens,
                            tokens,
                            part.head_start + h,
                            1 if fmt is KVCacheStoreFormat.HPLD else heads,
                            region_id,
                            l * layer_stride + h * head_stride,
                            token_stride,
                            head_stride,
                        )
                    )
    binding = KVCacheResolvedRuntimeBinding(
        operation,
        placement.resource_id,
        placement.placement_id,
        placement.digest,
        "instance",
        placement.revision,
        part.participant_id,
        None,
        None,
        tuple(regions),
        tuple(ranges),
    )
    return binding, buffers, expected


@pytest.mark.parametrize("fmt", tuple(KVCacheStoreFormat))
def test_real_memory_upload_discover_and_load_with_tp_pp_change(fmt):
    native = MemoryNative()
    a = _placement("a", (tuple(range(4)), tuple(range(4, 8)), tuple(range(8, 12))), 1)
    b = _placement("b", (tuple(range(6)), tuple(range(6, 12))), 2)
    for placement, page in ((a, "p0"), (b, "p1")):
        plan = plan_kv_cache_store_upload(placement, object_format=fmt)
        store = KVCacheStore(MemoryBackend(native), _manifest(plan.layout))
        store.register_layout()
        for part in placement.parts:
            binding, buffers, _ = memory_binding(
                placement, part.participant_id, fmt, 1, "upload"
            )
            assert store.upload(plan, (page,), binding, operation_id="upload") == (
                True,
            )
            assert buffers
    target = _placement("target", (tuple(range(6)), tuple(range(6, 12))), 2)
    local = _manifest(plan_kv_cache_store_upload(target, object_format=fmt).layout)
    reader = KVCacheStore(
        MemoryBackend(native),
        local,
        transfer_limits=KVCacheTransferLimits(
            max_batch_operations=3, max_batch_bytes=137
        ),
    )
    context = reader.discover(("p0", "p1", "missing"), operation_id="read")
    assert (
        len(context.page_keys) == 2 and context.layout_ids[0] != context.layout_ids[1]
    )
    assert native.manifest_batches == 1
    for part in target.parts:
        binding, buffers, expected = memory_binding(
            target, part.participant_id, fmt, 2, "read", fill=False
        )
        assert reader.load(context, target, binding) == 2
        # Each source uploaded a single page, so the expected page salt is zero.
        assert [bytes(buffer) for buffer in buffers] == expected[:2] * 2
    reader.discover(("p0", "p1"), operation_id="read-again")
    assert native.manifest_batches == 1


def test_parallel_registration_and_lost_reply_preserve_both_layouts():
    native = MemoryNative()
    native.barrier = Barrier(2)
    stores = [
        KVCacheStore(
            MemoryBackend(native),
            _manifest(
                plan_kv_cache_store_upload(
                    _placement(str(tp), ((0, 1), (2, 3)), tp),
                    object_format=KVCacheStoreFormat.PLHD,
                ).layout
            ),
        )
        for tp in (1, 2)
    ]
    with ThreadPoolExecutor(2) as executor:
        entries = list(executor.map(lambda s: s.register_layout(), stores))
    assert entries[0] != entries[1]
    data, _ = stores[0].backend.read_catalog(
        f"kv/{stores[0].manifest.model_domain}/layouts"
    )
    catalog = kv_cache_store_catalog_from_json(data.decode())
    assert len(catalog.entries) == 2
    native.lost_reply = True
    third = KVCacheStore(
        MemoryBackend(native),
        _manifest(
            plan_kv_cache_store_upload(
                _placement("third", ((0, 1, 2, 3),), 1),
                object_format=KVCacheStoreFormat.PLHD,
            ).layout
        ),
    )
    third.register_layout()
    data, _ = third.backend.read_catalog(catalog.catalog_key)
    assert len(kv_cache_store_catalog_from_json(data.decode()).entries) == 3


def test_eviction_short_reads_and_cross_operation_binding_do_not_activate_pages():
    native = MemoryNative()
    placement = _placement("source", ((0, 1),), 1)
    plan = plan_kv_cache_store_upload(placement, object_format=KVCacheStoreFormat.PLHD)
    store = KVCacheStore(MemoryBackend(native), _manifest(plan.layout))
    store.register_layout()
    source, buffers, _ = memory_binding(
        placement,
        placement.parts[0].participant_id,
        KVCacheStoreFormat.PLHD,
        2,
        "write",
    )
    assert store.upload(plan, ("p0", "p1"), source, operation_id="write") == (
        True,
        True,
    )
    context = store.discover(("p0", "p1"), operation_id="read")
    target, targets, _ = memory_binding(
        placement,
        placement.parts[0].participant_id,
        KVCacheStoreFormat.PLHD,
        2,
        "read",
        fill=False,
    )
    with pytest.raises(ValueError, match="operation_id"):
        store.load(context, placement, replace(target, operation_id="other"))
    native.short_key = store.manifest.object_keys("p1")[0]
    assert store.load(context, placement, target) == 1
    native.short_key = None
    del native.values[store.manifest.object_keys("p0")[0]]
    assert store.load(context, placement, target) == 0
    assert store.discover(("p0", "p1"), operation_id="fresh").page_keys == ()
    assert buffers and targets


def test_store_binding_does_not_relax_r2r_identity():
    placement = _placement("s", ((0, 1),), 1)
    binding, buffers, _ = memory_binding(
        placement, placement.parts[0].participant_id, KVCacheStoreFormat.PLHD, 1, "op"
    )
    with pytest.raises(ValueError, match="snapshot"):
        validate_resolved_runtime_binding(
            placement, _snapshot(token_count=16), binding, operation_id="op"
        )
    with pytest.raises(ValueError, match="snapshot"):
        kv_cache_resolved_binding_to_json(binding)
    with pytest.raises(ValueError, match="pair"):
        replace(binding, snapshot_id="partial")
    assert buffers


def test_wrong_catalog_manifest_or_capability_fails_before_transfer():
    native = MemoryNative()
    placement = _placement("s", ((0, 1),), 1)
    manifest = _manifest(
        plan_kv_cache_store_upload(
            placement, object_format=KVCacheStoreFormat.PLHD
        ).layout
    )
    store = KVCacheStore(MemoryBackend(native), manifest)
    native.values[manifest.manifest_key] = b"wrong"
    with pytest.raises(KVCacheStoreError, match="differs"):
        store.register_layout()
    with pytest.raises(KVCacheStoreError, match="capability"):
        KVCacheStore(object(), manifest)
    assert native.writes == 0 and native.reads == 0


def test_cancel_waits_for_submitted_native_read_and_returns_no_hit():
    native = MemoryNative()
    placement = _placement("s", ((0, 1),), 1)
    plan = plan_kv_cache_store_upload(placement, object_format=KVCacheStoreFormat.PLHD)
    store = KVCacheStore(
        MemoryBackend(native),
        _manifest(plan.layout),
        transfer_limits=KVCacheTransferLimits(max_batch_bytes=64),
    )
    writer = KVCacheStore(MemoryBackend(native), store.manifest)
    writer.register_layout()
    source, source_buffers, _ = memory_binding(
        placement, placement.parts[0].participant_id, KVCacheStoreFormat.PLHD, 1, "put"
    )
    writer.upload(plan, ("page",), source, operation_id="put")
    context = store.discover(("page",), operation_id="get")
    target, target_buffers, _ = memory_binding(
        placement,
        placement.parts[0].participant_id,
        KVCacheStoreFormat.PLHD,
        1,
        "get",
        fill=False,
    )
    entered, release, cancelled = Event(), Event(), Event()
    original = native.get_into_ranges

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    native.get_into_ranges = delayed
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            store.load, context, placement, target, cancelled=cancelled.is_set
        )
        assert entered.wait(5)
        cancelled.set()
        assert not future.done()
        release.set()
        assert future.result(timeout=5) == 0
    assert native.reads == 1
    assert (
        store.discover(
            ("page",), operation_id="cancelled", cancelled=lambda: True
        ).page_keys
        == ()
    )
    assert writer.upload(
        plan, ("other",), source, operation_id="put", cancelled=lambda: True
    ) == (False,)
    assert source_buffers and target_buffers


def test_immutable_manifest_publication_retries_in_progress_readback():
    native = MemoryNative()
    backend = MemoryBackend(native)
    original = native.get
    calls = []

    def delayed(key):
        calls.append(key)
        return b"" if len(calls) == 1 else original(key)

    native.get = delayed
    backend.publish_manifest("manifest", b"content", attempts=3)
    assert len(calls) == 2
    with pytest.raises(KVCacheStoreError, match="differs"):
        backend.publish_manifest("manifest", b"different", attempts=3)
