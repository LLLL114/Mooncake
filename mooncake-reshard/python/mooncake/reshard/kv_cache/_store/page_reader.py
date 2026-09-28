"""Compile one page once, then bind fresh page locations for each Store read."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, cast

from ...contracts import ParticipantId
from ..placement import KVCachePlacementManifest
from ..resolved import (
    KVCacheResolvedRuntimeBinding,
    _check_disjoint,
    _head_bytes,
    _physical_spans,
    _validated_range_spans,
)
from ..types import require_integer
from .backend import KVCacheStoreError
from .manifest import KVCacheStoreManifest
from .planner import plan_kv_cache_store_load
from .runtime import StoreByteRange, lower_store_ranges

if TYPE_CHECKING:
    from .store import KVCacheStore, KVCacheStoreReadContext


@dataclass(frozen=True)
class _ObjectRead:
    suffix: str
    region_id: str
    destinations: tuple[int, ...]
    sources: tuple[int, ...]
    sizes: tuple[int, ...]
    whole: bool


class KVCacheStorePageReader:
    """Reader for pages sharing a fixed, registered memory geometry.

    ``binding`` describes logical page zero. A load supplies, for each page,
    byte translations keyed by registered region ID. Only immutable geometry is
    cached; keys, source selection and translations belong to the current call.
    The caller owns registration and page protection until load returns. Rebuild
    this reader when the pool allocation, strides or target placement changes.
    """

    def __init__(
        self,
        store: KVCacheStore,
        placement: KVCachePlacementManifest,
        binding: KVCacheResolvedRuntimeBinding,
    ) -> None:
        if binding.snapshot_id is not None or binding.snapshot_digest is not None:
            raise ValueError("Store bindings must not carry snapshot identity")
        _validated_range_spans(
            placement,
            binding,
            binding.operation_id,
            0,
            placement.descriptor.page_size,
            store.transfer_limits,
        )
        self.store, self.placement, self.binding = store, placement, binding
        self.model_domain = store.manifest.model_domain
        self.regions = {r.region_id: r for r in binding.regions}
        self._templates: OrderedDict[str, tuple[_ObjectRead, ...]] = OrderedDict()
        self._lock = RLock()
        # Merge the destination footprint once. Dynamic overlap checks then
        # visit whole pages (or layer blocks), not every token/head range.
        footprints: dict[str, list[tuple[int, int]]] = {key: [] for key in self.regions}
        for item in binding.ranges:
            region = self.regions[item.region_id]
            footprints[item.region_id].extend(
                (start - region.address, end - region.address)
                for start, end in _physical_spans(
                    item,
                    region,
                    _head_bytes(placement, item.component),
                    store.transfer_limits.max_operations,
                )
            )
        self.footprints: dict[str, tuple[tuple[int, int], ...]] = {}
        for key, spans in footprints.items():
            merged: list[tuple[int, int]] = []
            for start, end in sorted(spans):
                if merged and merged[-1][1] == start:
                    merged[-1] = (merged[-1][0], end)
                else:
                    merged.append((start, end))
            self.footprints[key] = tuple(merged)

    def _compile(self, manifest: KVCacheStoreManifest) -> tuple[_ObjectRead, ...]:
        part = self.placement.part(ParticipantId(self.binding.participant_id))
        plan = plan_kv_cache_store_load(
            manifest.layout,
            self.placement,
            runtime_format=self.store.manifest.layout.object_format,
            target_dp_rank=part.rank.dp,
            limits=self.store.planning_limits,
        )
        fragments = tuple(
            r.fragment
            for r in plan.ranges
            if r.participant_id == self.binding.participant_id
        )
        records = lower_store_ranges(
            self.placement,
            self.binding,
            (fragments,),
            self.binding.operation_id,
            self.store.transfer_limits,
        )
        grouped: dict[tuple[int, str], list[StoreByteRange]] = {}
        for record in records:
            grouped.setdefault((record.object_index, record.region_id), []).append(
                record
            )
        suffixes = tuple(key[len("page") :] for key in manifest.object_keys("page"))
        object_sizes = manifest.layout.object_sizes
        limits = self.store.transfer_limits
        templates: list[_ObjectRead] = []
        for (obj, region), spans in grouped.items():
            # Split oversized spans and batches once, never in the hot path.
            chunks: list[list[tuple[int, int, int]]] = []
            chunk: list[tuple[int, int, int]] = []
            used = 0
            for span in spans:
                offset = 0
                while offset < span.nbytes:
                    if (
                        len(chunk) == limits.max_batch_operations
                        or used == limits.max_batch_bytes
                    ):
                        chunks.append(chunk)
                        chunk, used = [], 0
                    size = min(span.nbytes - offset, limits.max_batch_bytes - used)
                    chunk.append(
                        (span.region_offset + offset, span.object_offset + offset, size)
                    )
                    offset += size
                    used += size
            if chunk:
                chunks.append(chunk)
            for chunk in chunks:
                dst, src, sizes = tuple(zip(*chunk))
                cursor = 0
                whole = True
                for start, size in zip(src, sizes):
                    whole &= start == cursor
                    cursor += size
                whole &= cursor == object_sizes[obj]
                templates.append(
                    _ObjectRead(suffixes[obj], region, dst, src, sizes, whole)
                )
        return tuple(templates)

    def _validate_offsets(self, offsets: Sequence[Mapping[str, int]]) -> None:
        spans: list[tuple[int, int]] = []
        limits = self.store.transfer_limits
        for translations in offsets:
            if set(translations) != set(self.regions):
                raise ValueError("page translations differ from registered regions")
            for key, region in self.regions.items():
                delta = translations[key]
                require_integer(delta, "page byte translation")
                if delta % self.placement.descriptor.itemsize:
                    raise ValueError("page translation is not item aligned")
                for start, end in self.footprints[key]:
                    if end + delta > region.nbytes:
                        raise ValueError("page exceeds registered region bounds")
                    if len(spans) >= limits.max_operations:
                        raise ValueError("Store physical operation limit exceeded")
                    spans.append(
                        (region.address + start + delta, region.address + end + delta)
                    )
        _check_disjoint(spans, "page destinations")

    def load(
        self,
        context: KVCacheStoreReadContext,
        page_offsets: Sequence[Mapping[str, int]],
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> int:
        """Return the complete-page prefix; cancelled/failed pages are not hits."""
        if context.catalog.model_domain != self.model_domain:
            raise ValueError("read context model domain differs")
        offsets = tuple(page_offsets)
        if len(offsets) != len(context.page_keys):
            raise ValueError("page translations and read context differ")
        if not offsets or (cancelled is not None and cancelled()):
            return 0
        self._validate_offsets(offsets)
        templates: dict[str, tuple[_ObjectRead, ...]] = {}
        # One reader fixes the target placement and physical binding, so the
        # source layout ID fully identifies this reader's conversion template.
        with self._lock:
            for manifest in context.manifests:
                layout_id = manifest.layout.layout_id
                if layout_id not in self._templates:
                    self._templates[layout_id] = self._compile(manifest)
                self._templates.move_to_end(layout_id)
                templates[layout_id] = self._templates[layout_id]
                while len(self._templates) > self.store.cache_capacity:
                    self._templates.popitem(last=False)
        count = sum(len(r.sizes) for i in context.layout_ids for r in templates[i])
        size = sum(sum(r.sizes) for i in context.layout_ids for r in templates[i])
        limits = self.store.transfer_limits
        if count > limits.max_operations or size > limits.max_bytes:
            raise ValueError("Store transfer limit exceeded")
        read_ranges = self.store.backend.ranged_reader(
            key + template.suffix
            for key, layout_id in zip(context.page_keys, context.layout_ids)
            for template in templates[layout_id]
        )
        success = [True] * len(offsets)
        remaining = [len(templates[i]) for i in context.layout_ids]
        batch: list[tuple[int, str, int, _ObjectRead]] = []
        operations, used = 0, 0
        for page, (key, layout_id, translations) in enumerate(
            zip(context.page_keys, context.layout_ids, offsets)
        ):
            for template in templates[layout_id]:
                n, size = len(template.sizes), sum(template.sizes)
                if batch and (
                    operations + n > limits.max_batch_operations
                    or used + size > limits.max_batch_bytes
                ):
                    if cancelled is not None and cancelled():
                        return 0
                    self._read(batch, success, remaining, read_ranges)
                    batch, operations, used = [], 0, 0
                batch.append(
                    (
                        page,
                        key + template.suffix,
                        translations[template.region_id],
                        template,
                    )
                )
                operations += n
                used += size
        if batch and not (cancelled is not None and cancelled()):
            self._read(batch, success, remaining, read_ranges)
        if cancelled is not None and cancelled():
            return 0
        return next(
            (i for i, pending in enumerate(remaining) if pending or not success[i]),
            len(success),
        )

    def _read(
        self,
        batch: list[tuple[int, str, int, _ObjectRead]],
        success: list[bool],
        remaining: list[int],
        read_ranges: Callable[..., list[list[list[int]]]],
    ) -> None:
        native = self.store.backend.store
        whole = all(row[3].whole for row in batch)
        single = all(len(row[3].sizes) == 1 for row in batch)
        method = getattr(
            native, "batch_get_into" if single else "batch_get_into_multi_buffers", None
        )
        if whole and callable(method):
            method = cast(Callable[..., list[int]], method)
            whole_keys: list[str] = []
            pointers: list[list[int]] = []
            whole_sizes: list[list[int]] = []
            for _, key, delta, template in batch:
                base = self.regions[template.region_id].address + delta
                whole_keys.append(key)
                pointers.append([base + dst for dst in template.destinations])
                whole_sizes.append(list(template.sizes))
            results = (
                method(
                    whole_keys, [p[0] for p in pointers], [s[0] for s in whole_sizes]
                )
                if single
                else method(whole_keys, pointers, whole_sizes)
            )
            if len(results) != len(batch):
                raise KVCacheStoreError("native read result count differs")
            for (page, _, _, template), result in zip(batch, results):
                success[page] &= type(result) is int and result == sum(template.sizes)
                remaining[page] -= 1
            return
        rows: dict[str, list[tuple[int, str, int, _ObjectRead]]] = {}
        for page, key, delta, template in batch:
            rows.setdefault(template.region_id, []).append((page, key, delta, template))
        buffers = [self.regions[key].address for key in rows]
        keys = [[key for _, key, _, _ in row] for row in rows.values()]
        dst = [
            [[delta + d for d in t.destinations] for _, _, delta, t in row]
            for row in rows.values()
        ]
        src = [[list(t.sources) for _, _, _, t in row] for row in rows.values()]
        sizes = [[list(t.sizes) for _, _, _, t in row] for row in rows.values()]
        results = read_ranges(buffers, keys, dst, src, sizes)
        if len(results) != len(rows):
            raise KVCacheStoreError("native read result shape differs")
        for row, response in zip(rows.values(), results):
            if len(row) != len(response):
                raise KVCacheStoreError("native read key result shape differs")
            for (page, _, _, template), values in zip(row, response):
                if len(values) != len(template.sizes):
                    raise KVCacheStoreError("native read range result shape differs")
                success[page] &= all(
                    type(v) is int and v == size
                    for v, size in zip(values, template.sizes)
                )
                remaining[page] -= 1
