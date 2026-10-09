"""Deterministic buffers and a complete-object Part fake for Store tests."""

import ctypes

from mooncake.reshard.kv_cache import (
    KVCacheComponent,
    KVCacheRegisteredRegion,
    KVCacheResolvedRange,
    KVCacheResolvedRuntimeBinding,
    KVCacheStoreFormat,
    plan_kv_cache_store_upload,
)
from mooncake.reshard.kv_cache._store.backend import MultipartBackend
from test_store_contracts import _reference


class MemoryBackend(MultipartBackend):
    def metadata_config(self):
        return None


class MemoryNative:
    def __init__(self):
        self.values = {}
        self.parts = {}
        self.queries = self.manifest_reads = self.metadata_puts = self.range_reads = 0
        self.short_key = None

    def put(self, key, value, config):
        self.metadata_puts += 1
        self.values.setdefault(key, value)
        return 0

    def get(self, key):
        self.manifest_reads += 1
        return self.values.get(key, b"")

    def batch_put_parts_from(self, keys, index, count, manifest, pointers, sizes):
        result = []
        for key, ptrs, lengths in zip(keys, pointers, sizes):
            existing = self.parts.get(key)
            if existing is not None and all(v is not None for v in existing[1]):
                result.append(0)
                continue
            if existing is None:
                existing = self.parts[key] = (manifest, [None] * count)
            if existing[0] != manifest or len(existing[1]) != count:
                result.append(-1)
                continue
            if existing[1][index] is None:
                existing[1][index] = b"".join(
                    ctypes.string_at(p, n) for p, n in zip(ptrs, lengths)
                )
            result.append(0)
        return result

    def batch_query_parts(self, keys):
        self.queries += 1
        result = []
        for key in keys:
            entry = self.parts.get(key)
            result.append(
                (0, entry[0], len(entry[1]))
                if entry and all(v is not None for v in entry[1])
                else (-1, "", 0)
            )
        return result

    def prepare_get_into_ranges_template(self, dst, src, sizes):
        return (dst, src, sizes)

    def prepare_get_parts_snapshot(self, keys, manifests, counts):
        result = {}
        for key, ref, count in zip(keys, manifests, counts):
            entry = self.parts.get(key)
            if (
                entry
                and entry[0] == ref
                and len(entry[1]) == count
                and all(v is not None for v in entry[1])
            ):
                result.update(
                    {key + f"\x1fp{i}": value for i, value in enumerate(entry[1])}
                )
        return result

    def get_into_ranges_from_template(
        self, snapshot, templates, buffers, indices, keys, deltas, **kwargs
    ):
        self.range_reads += 1
        result = []
        for (dst, src, sizes), i, key, delta in zip(templates, indices, keys, deltas):
            data = snapshot.get(key)
            ok = data is not None and key != self.short_key
            if ok:
                for d, s, n in zip(dst, src, sizes):
                    assert s + n <= len(data)
                    ctypes.memmove(buffers[i] + delta + d, data[s : s + n], n)
            result.append(ok)
        return result


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
