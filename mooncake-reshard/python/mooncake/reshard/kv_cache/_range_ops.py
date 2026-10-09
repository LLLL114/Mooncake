"""Shared KV range geometry, independent of Store objects and TE endpoints.

Callers validate their bindings before entering this layer, then check the
compact copy's segment/byte counts against their own budgets before expansion.
Offsets remain relative to the owning object or registered region.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NamedTuple

from .resolved import KVCacheResolvedRange
from .types import KVCacheComponent


class KVCacheRangeGeometry(NamedTuple):
    token_start: int
    token_count: int
    head_start: int
    head_count: int
    offset_bytes: int
    token_stride_bytes: int
    head_stride_bytes: int


class KVCacheCopyGeometry(NamedTuple):
    source: KVCacheRangeGeometry | KVCacheResolvedRange
    target: KVCacheRangeGeometry | KVCacheResolvedRange
    token_start: int
    token_count: int
    head_start: int
    head_count: int
    token_step: int
    head_step: int
    head_bytes: int

    @property
    def segment_count(self) -> int:
        return (self.token_count // self.token_step) * (
            self.head_count // self.head_step
        )

    @property
    def nbytes(self) -> int:
        return self.token_count * self.head_count * self.head_bytes

    def segments(self) -> Iterator[tuple[int, int, int]]:
        """Yield source offset, target offset, length in token/head order."""
        source, target = self.source, self.target
        source_base = (
            source.offset_bytes
            + (self.token_start - source.token_start) * source.token_stride_bytes
            + (self.head_start - source.head_start) * source.head_stride_bytes
        )
        target_base = (
            target.offset_bytes
            + (self.token_start - target.token_start) * target.token_stride_bytes
            + (self.head_start - target.head_start) * target.head_stride_bytes
        )
        size = self.token_step * self.head_step * self.head_bytes
        for token in range(0, self.token_count, self.token_step):
            source_row = source_base + token * source.token_stride_bytes
            target_row = target_base + token * target.token_stride_bytes
            for head in range(0, self.head_count, self.head_step):
                yield (
                    source_row + head * source.head_stride_bytes,
                    target_row + head * target.head_stride_bytes,
                    size,
                )


def plan_range_copy(
    source: KVCacheRangeGeometry | KVCacheResolvedRange,
    target: KVCacheRangeGeometry | KVCacheResolvedRange,
    head_bytes: int,
    *,
    head_window: tuple[int, int] | None = None,
) -> KVCacheCopyGeometry | None:
    """Intersect logical ranges and coalesce only bytes contiguous on both sides."""
    start = max(source.token_start, target.token_start)
    end = min(
        source.token_start + source.token_count, target.token_start + target.token_count
    )
    h0 = max(source.head_start, target.head_start)
    h1 = min(
        source.head_start + source.head_count, target.head_start + target.head_count
    )
    if head_window is not None:
        h0, h1 = max(h0, head_window[0]), min(h1, head_window[1])
    if start >= end or h0 >= h1:
        return None
    head_step = (
        h1 - h0
        if source.head_stride_bytes == target.head_stride_bytes == head_bytes
        else 1
    )
    width = head_step * head_bytes
    token_step = (
        end - start
        if head_step == h1 - h0
        and source.token_stride_bytes == target.token_stride_bytes == width
        else 1
    )
    return KVCacheCopyGeometry(
        source,
        target,
        start,
        end - start,
        h0,
        h1 - h0,
        token_step,
        head_step,
        head_bytes,
    )


def index_runtime_ranges(
    ranges: tuple[KVCacheResolvedRange, ...],
) -> dict[tuple[int, KVCacheComponent], list[KVCacheResolvedRange]]:
    """Index each validated runtime range once, preserving its canonical order."""
    result: dict[tuple[int, KVCacheComponent], list[KVCacheResolvedRange]] = {}
    for item in ranges:
        result.setdefault((item.global_layer_id, item.component), []).append(item)
    return result
