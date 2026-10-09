"""Stable budget packing shared by TE and Store submission adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import TypeVar

_T = TypeVar("_T")


def iter_batches(
    items: Iterable[_T],
    *,
    cost: Callable[[_T], tuple[int, int]],
    max_operations: int,
    max_bytes: int,
    partition: Callable[[_T], object] | None = None,
) -> Iterator[list[_T]]:
    """Pack prevalidated atomic units by operation count, bytes, and partition.

    Callers split or reject oversized units before this step. Input ordering
    and atomicity are preserved; no transfer or completion policy lives here.
    """
    batch: list[_T] = []
    operations = used = 0
    current_partition: object = None
    for item in items:
        count, size = cost(item)
        key = None if partition is None else partition(item)
        if batch and (
            key != current_partition
            or operations + count > max_operations
            or used + size > max_bytes
        ):
            yield batch
            batch, operations, used = [], 0, 0
        batch.append(item)
        operations += count
        used += size
        current_partition = key
    if batch:
        yield batch
