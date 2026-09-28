"""Compile one page once, then bind fresh page locations for each Store read."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Any, cast

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
    native_template: Any = None


@dataclass(frozen=True)
class _NativePagePlan:
    handle: Any
    suffixes: tuple[str, ...]
    operations: int
    nbytes: int


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
        *,
        target_ordered: bool = False,
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
        regions = (
            sorted(binding.regions, key=lambda r: r.address)
            if target_ordered
            else binding.regions
        )
        self.regions = {r.region_id: r for r in regions}
        self._templates: OrderedDict[str, tuple[_ObjectRead, ...]] = OrderedDict()
        self._lock = RLock()
        self._prepare_native = getattr(
            store.backend.store, "prepare_get_into_ranges_template", None
        )
        native_read = getattr(
            store.backend.store, "get_into_ranges_from_template", None
        )
        self._native_templates = callable(self._prepare_native) and callable(
            native_read
        )
        if (
            self._prepare_native is not None or native_read is not None
        ) and not self._native_templates:
            raise KVCacheStoreError(
                "native Store must expose both ranged-read template methods"
            )
        # The optional native API has fixed materialization bounds. Preserve
        # explicitly larger Python batch limits through the original interface.
        self._native_templates = (
            self._native_templates
            and store.transfer_limits.max_batch_operations <= 100_000
            and store.transfer_limits.max_batch_bytes <= 1 << 40
            and len(self.regions) <= 100_000
        )
        self._native_staging = (
            getattr(store.backend.store, "supports_ranged_read_staging", False) is True
        )
        self.target_ordered = target_ordered
        self._plans: dict[str, _NativePagePlan] = {}
        self._prepare_plan = getattr(
            store.backend.store, "prepare_get_into_ranges_plan", None
        )
        self._native_plans = (
            target_ordered
            and self._native_templates
            and callable(self._prepare_plan)
            and callable(
                getattr(store.backend.store, "get_into_ranges_from_plan", None)
            )
            and getattr(store.backend.store, "supports_planned_range_reads", False)
            is True
        )
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
            target_ordered=self.target_ordered,
        )
        grouped: dict[tuple[int, str], list[StoreByteRange]] = {}
        runs: list[tuple[tuple[int, str], list[StoreByteRange]]] = []
        for record in records:
            key = (record.object_index, record.region_id)
            if self.target_ordered:
                # Keep source references inside a contiguous destination run.
                # Never regroup disjoint runs by source: that loses read order.
                if (
                    not runs
                    or runs[-1][0] != key
                    or runs[-1][1][-1].region_offset + runs[-1][1][-1].nbytes
                    != record.region_offset
                ):
                    runs.append((key, []))
                runs[-1][1].append(record)
            else:
                grouped.setdefault(key, []).append(record)
        if not self.target_ordered:
            runs = list(grouped.items())
        suffixes = tuple(key[len("page") :] for key in manifest.object_keys("page"))
        object_sizes = manifest.layout.object_sizes
        limits = self.store.transfer_limits
        templates: list[_ObjectRead] = []
        for (obj, region), spans in runs:
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
                    _ObjectRead(
                        suffixes[obj],
                        region,
                        dst,
                        src,
                        sizes,
                        whole,
                        (
                            cast(Callable[..., Any], self._prepare_native)(
                                dst, src, sizes
                            )
                            if self._native_templates
                            else None
                        ),
                    )
                )
        operations = sum(len(t.sizes) for t in templates)
        nbytes = sum(sum(t.sizes) for t in templates)
        if (
            self._native_plans
            and operations <= limits.max_batch_operations
            and nbytes <= limits.max_batch_bytes
        ):
            regions = {key: i for i, key in enumerate(self.regions)}
            self._plans[manifest.layout.layout_id] = _NativePagePlan(
                cast(Callable[..., Any], self._prepare_plan)(
                    [t.native_template for t in templates],
                    [t.suffix for t in templates],
                    [regions[t.region_id] for t in templates],
                ),
                tuple(dict.fromkeys(t.suffix for t in templates)),
                operations,
                nbytes,
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
        plans: dict[str, _NativePagePlan] = {}
        # One reader fixes the target placement and physical binding, so the
        # source layout ID fully identifies this reader's conversion template.
        with self._lock:
            for manifest in context.manifests:
                layout_id = manifest.layout.layout_id
                if layout_id not in self._templates:
                    self._templates[layout_id] = self._compile(manifest)
                self._templates.move_to_end(layout_id)
                templates[layout_id] = self._templates[layout_id]
                if layout_id in self._plans:
                    plans[layout_id] = self._plans[layout_id]
                while len(self._templates) > self.store.cache_capacity:
                    evicted, _ = self._templates.popitem(last=False)
                    self._plans.pop(evicted, None)
        if len(plans) == len(templates):
            return self._load_plans(context, offsets, plans, cancelled)
        count = sum(len(r.sizes) for i in context.layout_ids for r in templates[i])
        size = sum(sum(r.sizes) for i in context.layout_ids for r in templates[i])
        limits = self.store.transfer_limits
        if count > limits.max_operations or size > limits.max_bytes:
            raise ValueError("Store transfer limit exceeded")
        read_ranges = self.store.backend.ranged_reader(
            (
                key + template.suffix
                for key, layout_id in zip(context.page_keys, context.layout_ids)
                for template in templates[layout_id]
            ),
            templates=self._native_templates,
        )
        success = [True] * len(offsets)
        remaining = [len(templates[i]) for i in context.layout_ids]
        batch: list[tuple[int, str, int, _ObjectRead]] = []
        operations, used = 0, 0
        bound = (
            (page, key + template.suffix, translations[template.region_id], template)
            for page, (key, layout_id, translations) in enumerate(
                zip(context.page_keys, context.layout_ids, offsets)
            )
            for template in templates[layout_id]
        )
        if self.target_ordered:
            # Templates contain contiguous destinations, but physical host pages
            # can be out of order. Bind and order runs before cutting batches.
            bound = sorted(
                bound,
                key=lambda row: (
                    self.regions[row[3].region_id].address,
                    row[2] + row[3].destinations[0],
                ),
            )
        for row in bound:
            template = row[3]
            n, size = len(template.sizes), sum(template.sizes)
            if batch and (
                operations + n > limits.max_batch_operations
                or used + size > limits.max_batch_bytes
            ):
                if cancelled is not None and cancelled():
                    return 0
                self._read(batch, success, remaining, read_ranges)
                batch, operations, used = [], 0, 0
            batch.append(row)
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

    def _load_plans(
        self,
        context: KVCacheStoreReadContext,
        offsets: tuple[Mapping[str, int], ...],
        plans: dict[str, _NativePagePlan],
        cancelled: Callable[[], bool] | None,
    ) -> int:
        pages = [plans[key] for key in context.layout_ids]
        limits = self.store.transfer_limits
        if (
            sum(p.operations for p in pages) > limits.max_operations
            or sum(p.nbytes for p in pages) > limits.max_bytes
        ):
            raise ValueError("Store transfer limit exceeded")
        read = self.store.backend.ranged_reader(
            (
                key + suffix
                for key, plan in zip(context.page_keys, pages)
                for suffix in plan.suffixes
            ),
            plans=True,
        )
        buffers = [r.address for r in self.regions.values()]
        translations = [[offset[key] for key in self.regions] for offset in offsets]
        order = sorted(range(len(pages)), key=lambda i: translations[i])
        complete = [False] * len(pages)

        def execute(batch: list[int]) -> None:
            results = read(
                [pages[i].handle for i in batch],
                buffers,
                [context.page_keys[i] for i in batch],
                [translations[i] for i in batch],
                allow_staging=self._native_staging,
            )
            if len(results) != len(batch) or any(type(v) is not bool for v in results):
                raise KVCacheStoreError("native plan read result shape differs")
            for i, ok in zip(batch, results):
                complete[i] = ok

        batch: list[int] = []
        operations, nbytes = 0, 0
        for i in order:
            plan = pages[i]
            if batch and (
                operations + plan.operations > limits.max_batch_operations
                or nbytes + plan.nbytes > limits.max_batch_bytes
            ):
                if cancelled is not None and cancelled():
                    return 0
                execute(batch)
                batch, operations, nbytes = [], 0, 0
            batch.append(i)
            operations += plan.operations
            nbytes += plan.nbytes
        if batch and not (cancelled is not None and cancelled()):
            execute(batch)
        if cancelled is not None and cancelled():
            return 0
        return next((i for i, ok in enumerate(complete) if not ok), len(complete))

    def _read(
        self,
        batch: list[tuple[int, str, int, _ObjectRead]],
        success: list[bool],
        remaining: list[int],
        read_ranges: Callable[..., Any],
    ) -> None:
        native = self.store.backend.store
        whole = all(row[3].whole for row in batch)
        single = all(len(row[3].sizes) == 1 for row in batch)
        method = getattr(
            native, "batch_get_into" if single else "batch_get_into_multi_buffers", None
        )
        if whole and callable(method) and not self.target_ordered:
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
        if self._native_templates:
            regions = {key: i for i, key in enumerate(self.regions)}
            results = read_ranges(
                [row[3].native_template for row in batch],
                [region.address for region in self.regions.values()],
                [regions[row[3].region_id] for row in batch],
                [row[1] for row in batch],
                [row[2] for row in batch],
                **({"allow_staging": True} if self._native_staging else {}),
            )
            if len(results) != len(batch) or any(type(v) is not bool for v in results):
                raise KVCacheStoreError("native template read result shape differs")
            for (page, _, _, _), complete in zip(batch, results):
                success[page] &= complete
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
