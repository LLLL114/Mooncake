"""Synchronous source-native KV Store upload and per-page discovered restore."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from threading import RLock
from typing import TYPE_CHECKING, Any

from ...contracts import ParticipantId
from ..placement import KVCachePlacementManifest
from ..resolved import (
    DEFAULT_TRANSFER_LIMITS,
    KVCacheResolvedRuntimeBinding,
    KVCacheTransferLimits,
)
from ..types import require_integer, require_manifest_items, require_nonempty_string
from .backend import KVCacheStoreError, StoreBackend
from .catalog import KVCacheStoreLayoutCatalog, KVCacheStoreLayoutEntry
from .discovery import plan_kv_cache_store_probe, select_kv_cache_store_sources
from .manifest import KVCacheStoreManifest
from .planner import (
    DEFAULT_STORE_PLANNING_LIMITS,
    KVCacheStoreLoadPlan,
    KVCacheStorePlanningLimits,
    KVCacheStoreUploadPlan,
    plan_kv_cache_store_load,
)
from .runtime import StoreByteRange, lower_store_ranges
from .serde import (
    kv_cache_store_catalog_from_json,
    kv_cache_store_catalog_to_json,
    kv_cache_store_manifest_from_json,
    kv_cache_store_manifest_to_json,
)

if TYPE_CHECKING:
    from .page_reader import KVCacheStorePageReader


@dataclass(frozen=True)
class KVCacheStoreReadContext:
    operation_id: str
    catalog: KVCacheStoreLayoutCatalog
    page_keys: tuple[str, ...]
    layout_ids: tuple[str, ...]
    manifests: tuple[KVCacheStoreManifest, ...]

    def __post_init__(self) -> None:
        require_nonempty_string(self.operation_id, "operation_id")
        if not isinstance(self.catalog, KVCacheStoreLayoutCatalog):
            raise ValueError(
                "catalog must be a KVCacheStoreLayoutCatalog"
            )  # noqa: TRY004
        pages = require_manifest_items(self.page_keys, "page_keys", str)
        for key in pages:
            require_nonempty_string(key, "page_key")
        ids = require_manifest_items(self.layout_ids, "layout_ids", str)
        manifests = require_manifest_items(
            self.manifests, "manifests", KVCacheStoreManifest
        )
        by_id = {m.layout.layout_id: m for m in manifests}
        if (
            len(pages) != len(ids)
            or len(by_id) != len(manifests)
            or set(by_id) != set(ids)
        ):
            raise ValueError("read context pages and selected manifests differ")
        for manifest in manifests:
            self.catalog.validate_manifest(manifest)
        object.__setattr__(self, "page_keys", pages)
        object.__setattr__(self, "layout_ids", ids)
        object.__setattr__(self, "manifests", manifests)


def _pages(keys: Sequence[str]) -> tuple[str, ...]:
    pages = require_manifest_items(keys, "page_keys", str)
    for key in pages:
        require_nonempty_string(key, "page_key")
    return pages


def _read_catalog(
    backend: StoreBackend, domain: str
) -> tuple[KVCacheStoreLayoutCatalog, str]:
    empty = KVCacheStoreLayoutCatalog(domain)
    data, token = backend.read_catalog(empty.catalog_key)
    catalog = (
        empty
        if data is None
        else kv_cache_store_catalog_from_json(data.decode("utf-8"))
    )
    if catalog.model_domain != domain:
        raise KVCacheStoreError("catalog model domain differs")
    return catalog, token


def _range_batches(
    records: tuple[StoreByteRange, ...], limits: KVCacheTransferLimits
) -> Iterator[list[StoreByteRange]]:
    batch: list[StoreByteRange] = []
    used = 0
    for record in records:
        offset = 0
        while offset < record.nbytes:
            if (
                len(batch) == limits.max_batch_operations
                or used == limits.max_batch_bytes
            ):
                yield batch
                batch, used = [], 0
            size = min(record.nbytes - offset, limits.max_batch_bytes - used)
            batch.append(
                replace(
                    record,
                    object_offset=record.object_offset + offset,
                    region_offset=record.region_offset + offset,
                    nbytes=size,
                )
            )
            used += size
            offset += size
    if batch:
        yield batch


class KVCacheStore:
    """Framework-owned allocations must stay registered and stable during calls.

    This object owns bounded metadata caches, never framework pages. All native
    transfers finish before upload/load returns; there is no background owner.
    """

    def __init__(
        self,
        backend: Any,
        manifest: KVCacheStoreManifest,
        *,
        planning_limits: KVCacheStorePlanningLimits = DEFAULT_STORE_PLANNING_LIMITS,
        transfer_limits: KVCacheTransferLimits = DEFAULT_TRANSFER_LIMITS,
        cache_capacity: int = 64,
        registration_attempts: int = 8,
        page_batch_size: int = 128,
    ) -> None:
        if not isinstance(manifest, KVCacheStoreManifest):
            raise ValueError("local manifest is required")  # noqa: TRY004
        backend = (
            backend if isinstance(backend, StoreBackend) else StoreBackend(backend)
        )
        if not isinstance(
            planning_limits, KVCacheStorePlanningLimits
        ) or not isinstance(transfer_limits, KVCacheTransferLimits):
            raise ValueError("invalid Store limits")  # noqa: TRY004
        for name, value in (
            ("cache_capacity", cache_capacity),
            ("registration_attempts", registration_attempts),
            ("page_batch_size", page_batch_size),
        ):
            require_integer(value, name, minimum=1)
        self.backend = backend
        self.manifest = manifest
        self.planning_limits = planning_limits
        self.transfer_limits = transfer_limits
        self.cache_capacity = cache_capacity
        self.registration_attempts = registration_attempts
        self.page_batch_size = page_batch_size
        self._manifests: OrderedDict[str, KVCacheStoreManifest] = OrderedDict()
        self._plans: OrderedDict[tuple[str, str, int], KVCacheStoreLoadPlan] = (
            OrderedDict()
        )
        self._lock = RLock()
        self._catalog_cache: (
            tuple[bytes | None, str, KVCacheStoreLayoutCatalog] | None
        ) = None

    def register_layout(self) -> KVCacheStoreLayoutEntry:
        catalog, token = _read_catalog(self.backend, self.manifest.model_domain)
        self.backend.publish_manifest(
            self.manifest.manifest_key,
            kv_cache_store_manifest_to_json(self.manifest).encode(),
            self.registration_attempts,
        )
        entry = KVCacheStoreLayoutEntry.from_manifest(self.manifest)
        for attempt in range(self.registration_attempts):
            updated = catalog.with_manifest(self.manifest)
            if updated == catalog:
                return entry
            try:
                if self.backend.compare_catalog(
                    catalog.catalog_key,
                    token,
                    kv_cache_store_catalog_to_json(updated).encode(),
                ):
                    return entry
            except KVCacheStoreError:
                # A successful commit can lose its reply. Confirm the semantic result.
                observed, _ = _read_catalog(self.backend, self.manifest.model_domain)
                if entry in observed.entries:
                    return entry
                raise
            if attempt + 1 < self.registration_attempts:
                catalog, token = _read_catalog(self.backend, self.manifest.model_domain)
        raise KVCacheStoreError("layout registration conflict retry limit exceeded")

    def prepare_page_reader(
        self,
        target_placement: KVCachePlacementManifest,
        page_binding: KVCacheResolvedRuntimeBinding,
        *,
        target_ordered: bool = False,
    ) -> KVCacheStorePageReader:
        """Prepare repeated-page reads for one fixed registered runtime pool.

        The binding covers one complete logical page. The returned reader
        caches source-to-target byte templates and accepts fresh per-region
        page offsets on each load. Recreate it when the pool changes.

        target_ordered plans contiguous destination runs before batching. Receiver
        staging remains an independent policy. A gather-capable engine can pack
        contiguous target runs on the owner; that path requires destination
        host buffers registered with register_buffer_for_remote_access before
        starting IO. Engines without gather retain destination-ordered reads.
        """
        from .page_reader import KVCacheStorePageReader

        return KVCacheStorePageReader(
            self, target_placement, page_binding, target_ordered=target_ordered
        )

    def upload(
        self,
        plan: KVCacheStoreUploadPlan,
        page_keys: Sequence[str],
        binding: KVCacheResolvedRuntimeBinding,
        *,
        operation_id: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[bool, ...]:
        if (
            not isinstance(plan, KVCacheStoreUploadPlan)
            or plan.layout != self.manifest.layout
        ):
            raise ValueError("upload plan differs from local layout")
        pages = _pages(page_keys)
        if not pages:
            return ()
        if cancelled is not None and cancelled():
            return (False,) * len(pages)
        fragments = tuple(
            f
            for f in plan.layout.fragments
            if plan.object_writers[f.object_index] == binding.participant_id
        )
        records = lower_store_ranges(
            plan.source_placement,
            binding,
            (fragments,) * len(pages),
            operation_id,
            self.transfer_limits,
        )
        grouped: dict[tuple[int, int], list[StoreByteRange]] = {}
        for record in records:
            grouped.setdefault((record.page_index, record.object_index), []).append(
                record
            )
        owned = [
            i
            for i, writer in enumerate(plan.object_writers)
            if writer == binding.participant_id
        ]
        if len(grouped) != len(pages) * len(owned):
            raise ValueError("upload object coverage is incomplete")
        regions = {r.region_id: r for r in binding.regions}
        key_rows = [self.manifest.object_keys(page) for page in pages]
        batches: list[list[tuple[int, int]]] = []
        batch: list[tuple[int, int]] = []
        size = 0
        operations = 0
        for item, spans in grouped.items():
            cursor = 0
            for span in spans:
                if span.object_offset != cursor:
                    raise ValueError("upload object has a byte gap or overlap")
                cursor += span.nbytes
            if cursor != plan.layout.object_sizes[item[1]]:
                raise ValueError("upload object size differs")
            if (
                cursor > self.transfer_limits.max_batch_bytes
                or len(spans) > self.transfer_limits.max_batch_operations
            ):
                raise ValueError("one upload object exceeds native batch limits")
            if batch and (
                size + cursor > self.transfer_limits.max_batch_bytes
                or operations + len(spans) > self.transfer_limits.max_batch_operations
            ):
                batches.append(batch)
                batch, size, operations = [], 0, 0
            batch.append(item)
            size += cursor
            operations += len(spans)
        if batch:
            batches.append(batch)
        success = [True] * len(pages)
        completed = [0] * len(pages)
        for batch in batches:
            if cancelled is not None and cancelled():
                break
            keys = [key_rows[p][o] for p, o in batch]
            ptrs = [
                [regions[r.region_id].address + r.region_offset for r in grouped[item]]
                for item in batch
            ]
            sizes = [[r.nbytes for r in grouped[item]] for item in batch]
            if all(len(row) == 1 for row in ptrs) and callable(
                getattr(self.backend.store, "batch_put_from", None)
            ):
                result = self.backend.store.batch_put_from(
                    keys,
                    [r[0] for r in ptrs],
                    [r[0] for r in sizes],
                    self.backend.payload_config(),
                )
            else:
                result = self.backend.store.batch_put_from_multi_buffers(
                    keys, ptrs, sizes, self.backend.payload_config()
                )
            if len(result) != len(batch) or any(type(v) is not int for v in result):
                raise KVCacheStoreError("invalid native upload result")
            failed = [i for i, status in enumerate(result) if status != 0]
            if failed:
                exists = self.backend.store.batch_is_exist([keys[i] for i in failed])
                if len(exists) != len(failed):
                    raise KVCacheStoreError("upload readback result count differs")
                for i, status in zip(failed, exists):
                    if type(status) is int and status == 1:
                        result[i] = 0
            for (page, _), status in zip(batch, result):
                success[page] &= status == 0
                completed[page] += 1
        return tuple(
            ok and count == len(owned) for ok, count in zip(success, completed)
        )

    def discover(
        self,
        page_keys: Sequence[str],
        *,
        operation_id: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> KVCacheStoreReadContext:
        require_nonempty_string(operation_id, "operation_id")
        pages = _pages(page_keys)
        if cancelled is not None and cancelled():
            return KVCacheStoreReadContext(
                operation_id,
                KVCacheStoreLayoutCatalog(self.manifest.model_domain),
                (),
                (),
                (),
            )
        # Read the version every time; only immutable parsing/validation is cached.
        domain = self.manifest.model_domain
        data, token = self.backend.read_catalog(
            KVCacheStoreLayoutCatalog(domain).catalog_key
        )
        with self._lock:
            cached_catalog = self._catalog_cache
            if cached_catalog is not None and cached_catalog[:2] == (data, token):
                catalog = cached_catalog[2]
            else:
                catalog = (
                    KVCacheStoreLayoutCatalog(domain)
                    if data is None
                    else kv_cache_store_catalog_from_json(data.decode("utf-8"))
                )
                if catalog.model_domain != domain:
                    raise KVCacheStoreError("catalog model domain differs")
                self._catalog_cache = (data, token, catalog)
        with self._lock:
            cached = tuple(self._manifests)
        preferred = tuple(dict.fromkeys((self.manifest.layout.layout_id, *cached)))
        selected: list[str] = []
        for start in range(0, len(pages), self.page_batch_size):
            if cancelled is not None and cancelled():
                return KVCacheStoreReadContext(operation_id, catalog, (), (), ())
            subset = pages[start : start + self.page_batch_size]
            probe = plan_kv_cache_store_probe(
                catalog, subset, limits=self.planning_limits
            )
            results = (
                self.backend.store.batch_is_exist(probe.keys) if probe.keys else ()
            )
            chosen = select_kv_cache_store_sources(
                probe, results, preferred_layout_ids=preferred
            )
            selected.extend(chosen)
            if len(chosen) < len(subset):
                break
        entries = {e.layout_id: e for e in catalog.entries}
        chosen_ids = tuple(dict.fromkeys(selected))
        found: dict[str, KVCacheStoreManifest] = {}
        with self._lock:
            for layout_id in chosen_ids:
                if layout_id in self._manifests:
                    found[layout_id] = self._manifests[layout_id]
                    self._manifests.move_to_end(layout_id)
        missing = [i for i in chosen_ids if i not in found]
        if missing:
            values = self.backend.get_manifests(
                [entries[i].manifest_key(catalog.model_domain) for i in missing]
            )
            for layout_id, value in zip(missing, values):
                manifest = kv_cache_store_manifest_from_json(value.decode("utf-8"))
                catalog.validate_manifest(manifest)
                if manifest.layout.layout_id != layout_id:
                    raise KVCacheStoreError(
                        "manifest response differs from requested layout"
                    )
                found[layout_id] = manifest
                with self._lock:
                    self._manifests[layout_id] = manifest
                    while len(self._manifests) > self.cache_capacity:
                        self._manifests.popitem(last=False)
        if cancelled is not None and cancelled():
            return KVCacheStoreReadContext(operation_id, catalog, (), (), ())
        return KVCacheStoreReadContext(
            operation_id,
            catalog,
            pages[: len(selected)],
            tuple(selected),
            tuple(found[i] for i in chosen_ids),
        )

    def load(
        self,
        context: KVCacheStoreReadContext,
        target_placement: KVCachePlacementManifest,
        binding: KVCacheResolvedRuntimeBinding,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> int:
        if context.catalog.model_domain != self.manifest.model_domain:
            raise ValueError("read context model domain differs")
        if not context.page_keys or (cancelled is not None and cancelled()):
            return 0
        target_dp = target_placement.part(ParticipantId(binding.participant_id)).rank.dp
        by_id = {m.layout.layout_id: m for m in context.manifests}
        plans: dict[str, KVCacheStoreLoadPlan] = {}
        for layout_id, manifest in by_id.items():
            key = (layout_id, target_placement.digest, target_dp)
            with self._lock:
                plan = self._plans.get(key)
            if plan is None:
                plan = plan_kv_cache_store_load(
                    manifest.layout,
                    target_placement,
                    runtime_format=self.manifest.layout.object_format,
                    target_dp_rank=target_dp,
                    limits=self.planning_limits,
                )
                with self._lock:
                    self._plans[key] = plan
                    while len(self._plans) > self.cache_capacity:
                        self._plans.popitem(last=False)
            plans[layout_id] = plan
        fragments = tuple(
            tuple(
                r.fragment
                for r in plans[i].ranges
                if r.participant_id == binding.participant_id
            )
            for i in context.layout_ids
        )
        records = lower_store_ranges(
            target_placement,
            binding,
            fragments,
            context.operation_id,
            self.transfer_limits,
        )
        key_rows = [
            by_id[i].object_keys(page)
            for i, page in zip(context.layout_ids, context.page_keys)
        ]
        regions = {r.region_id: r for r in binding.regions}
        success = [True] * len(context.page_keys)
        needed = [0] * len(context.page_keys)
        completed = [0] * len(context.page_keys)
        for record in records:
            needed[record.page_index] += record.nbytes
        read_ranges = self.backend.ranged_reader(
            key_rows[r.page_index][r.object_index] for r in records
        )
        for batch in _range_batches(records, self.transfer_limits):
            if cancelled is not None and cancelled():
                return 0
            rows: dict[str, dict[str, list[StoreByteRange]]] = {}
            for record in batch:
                key = key_rows[record.page_index][record.object_index]
                rows.setdefault(record.region_id, {}).setdefault(key, []).append(record)
            buffers = [regions[i].address for i in rows]
            all_keys = [list(row) for row in rows.values()]
            all_spans = [list(row.values()) for row in rows.values()]
            dst = [
                [[r.region_offset for r in spans] for spans in row] for row in all_spans
            ]
            src = [
                [[r.object_offset for r in spans] for spans in row] for row in all_spans
            ]
            sizes = [[[r.nbytes for r in spans] for spans in row] for row in all_spans]
            results = read_ranges(buffers, all_keys, dst, src, sizes)
            if len(results) != len(all_spans):
                raise KVCacheStoreError("native read result shape differs")
            for row, response in zip(all_spans, results):
                if len(row) != len(response):
                    raise KVCacheStoreError("native read key result shape differs")
                for spans, values in zip(row, response):
                    if len(spans) != len(values):
                        raise KVCacheStoreError(
                            "native read range result shape differs"
                        )
                    for span, value in zip(spans, values):
                        if type(value) is not int or value != span.nbytes:
                            success[span.page_index] = False
                        else:
                            completed[span.page_index] += span.nbytes
        if cancelled is not None and cancelled():
            return 0
        success = [
            ok and done == total for ok, done, total in zip(success, completed, needed)
        ]
        return next((i for i, ok in enumerate(success) if not ok), len(success))


__all__ = ["KVCacheStore", "KVCacheStoreReadContext"]
