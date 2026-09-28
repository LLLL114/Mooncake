"""Lower fixed-page Store geometry into checked local memory byte ranges."""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..placement import KVCachePlacementManifest
from ..resolved import (
    KVCacheResolvedRange,
    KVCacheResolvedRuntimeBinding,
    KVCacheTransferLimits,
    _validated_range_spans,
)
from ..types import KVCacheComponent
from .manifest import KVCacheStoreFragment


@dataclass(frozen=True)
class StoreByteRange:
    page_index: int
    object_index: int
    object_offset: int
    region_id: str
    region_offset: int
    nbytes: int


def lower_store_ranges(
    placement: KVCachePlacementManifest,
    binding: KVCacheResolvedRuntimeBinding,
    fragments: tuple[tuple[KVCacheStoreFragment, ...], ...],
    operation_id: str,
    limits: KVCacheTransferLimits,
    *,
    target_ordered: bool = False,
) -> tuple[StoreByteRange, ...]:
    """Validate all runtime pages, then join each page's selected Store layout."""
    if binding.snapshot_id is not None or binding.snapshot_digest is not None:
        raise ValueError("Store bindings must not carry snapshot identity")
    page_size = placement.descriptor.page_size
    _validated_range_spans(
        placement, binding, operation_id, 0, len(fragments) * page_size, limits
    )
    by_layer: dict[tuple[int, KVCacheComponent], list[KVCacheResolvedRange]] = {}
    for item in binding.ranges:
        by_layer.setdefault((item.global_layer_id, item.component), []).append(item)
    regions = {r.region_id: r for r in binding.regions}
    records: list[StoreByteRange] = []
    useful_bytes = 0
    for page_index, page_fragments in enumerate(fragments):
        page_start = page_index * page_size
        for fragment in page_fragments:
            d = placement.descriptor
            dim = (
                d.key_head_dim
                if fragment.component is KVCacheComponent.KEY
                else d.value_head_dim
            )
            head_bytes = dim * d.itemsize
            for item in by_layer.get(
                (fragment.global_layer_id, fragment.component), []
            ):
                begin = max(page_start, item.token_start)
                end = min(page_start + page_size, item.token_end)
                h0 = max(fragment.head_start, item.head_start)
                h1 = min(fragment.head_start + fragment.head_count, item.head_end)
                if begin >= end or h0 >= h1:
                    continue
                head_step = (
                    h1 - h0
                    if item.head_stride_bytes
                    == fragment.head_stride_bytes
                    == head_bytes
                    else 1
                )
                width = head_step * head_bytes
                token_step = (
                    end - begin
                    if (
                        head_step == h1 - h0
                        and item.token_stride_bytes
                        == fragment.token_stride_bytes
                        == width
                    )
                    else 1
                )
                count = ((end - begin) // token_step) * ((h1 - h0) // head_step)
                if len(records) + count > limits.max_operations:
                    raise ValueError("Store physical operation limit exceeded")
                useful_bytes += (end - begin) * (h1 - h0) * head_bytes
                if useful_bytes > limits.max_bytes:
                    raise ValueError("Store transfer byte limit exceeded")
                for token in range(begin, end, token_step):
                    for head in range(h0, h1, head_step):
                        records.append(
                            StoreByteRange(
                                page_index,
                                fragment.object_index,
                                fragment.object_offset
                                + (token - page_start) * fragment.token_stride_bytes
                                + (head - fragment.head_start)
                                * fragment.head_stride_bytes,
                                item.region_id,
                                item.address(regions[item.region_id], token, head)
                                - regions[item.region_id].address,
                                token_step * width,
                            )
                        )
    records.sort(
        key=(
            (lambda r: (r.region_id, r.region_offset))
            if target_ordered
            else lambda r: (
                r.page_index,
                r.object_index,
                r.object_offset,
                r.region_id,
                r.region_offset,
            )
        )
    )
    merged: list[StoreByteRange] = []
    for record in records:
        if merged:
            last = merged[-1]
            if (
                (last.page_index, last.object_index, last.region_id)
                == (record.page_index, record.object_index, record.region_id)
                and last.object_offset + last.nbytes == record.object_offset
                and last.region_offset + last.nbytes == record.region_offset
            ):
                merged[-1] = replace(last, nbytes=last.nbytes + record.nbytes)
                continue
        merged.append(record)
    return tuple(merged)
