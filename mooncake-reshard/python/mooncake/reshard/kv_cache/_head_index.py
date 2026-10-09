"""Head-interval lookup shared by runtime routing and Store reconstruction."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Iterator
from typing import Generic, TypeVar

_T = TypeVar("_T")


class HeadIntervalIndex(Generic[_T]):
    """Index validated, disjoint intervals with optional exact-interval replicas.

    Candidates within an interval retain caller order. Runtime selects a
    declared replica; Store expects one source fragment. This index owns neither
    selection policy nor topology, object keys, or runtime addresses.
    """

    def __init__(self, entries: Iterable[tuple[int, int, _T]]) -> None:
        grouped: dict[tuple[int, int], list[_T]] = {}
        for start, end, value in entries:
            grouped.setdefault((start, end), []).append(value)
        self._runs = tuple(
            (start, end, tuple(grouped[(start, end)])) for start, end in sorted(grouped)
        )
        for previous, current in zip(self._runs, self._runs[1:]):
            if previous[1] > current[0]:
                raise ValueError(
                    "overlapping source head intervals without exact replicas"
                )
        self._starts = tuple(run[0] for run in self._runs)

    def cover(self, start: int, end: int) -> Iterator[tuple[int, int, tuple[_T, ...]]]:
        """Yield a complete clipped cover without visiting individual heads."""
        index = max(0, bisect_right(self._starts, start) - 1)
        cursor = start
        while index < len(self._runs) and self._runs[index][0] < end:
            lo, hi, candidates = self._runs[index]
            lo, hi = max(start, lo), min(end, hi)
            if lo != cursor or hi <= lo:
                raise ValueError("source head coverage has a gap")
            yield lo, hi, candidates
            cursor = hi
            index += 1
        if cursor != end:
            raise ValueError("source head coverage is incomplete")
