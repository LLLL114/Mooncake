"""One immutable source layout per page object; no catalog or CAS publication."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import RLock
from typing import Any

from ..placement import KVCachePlacementManifest
from ..resolved import DEFAULT_TRANSFER_LIMITS, KVCacheResolvedRuntimeBinding
from ..types import require_integer, require_manifest_items, require_nonempty_string
from .backend import KVCacheStoreError, MultipartBackend
from .manifest import KVCacheStoreManifest
from .page_reader import MultipartPageReader
from .planner import DEFAULT_STORE_PLANNING_LIMITS, KVCacheStoreUploadPlan
from .runtime import lower_store_ranges
from .serde import kv_cache_store_manifest_from_json, kv_cache_store_manifest_to_json


@dataclass(frozen=True)
class MultipartReadContext:
    operation_id: str
    model_domain: str
    page_keys: tuple[str, ...]
    layout_ids: tuple[str, ...]
    manifests: tuple[KVCacheStoreManifest, ...]

    def __post_init__(self) -> None:
        require_nonempty_string(self.operation_id, "operation_id")
        require_manifest_items(self.page_keys, "page_keys", str)
        for key in self.page_keys:
            require_nonempty_string(key, "page_key")
        by_id = {m.layout.layout_id: m for m in self.manifests}
        if not self.operation_id or len(self.page_keys) != len(self.layout_ids):
            raise ValueError("invalid multipart read context")
        if len(by_id) != len(self.manifests) or set(by_id) != set(self.layout_ids):
            raise ValueError("multipart source manifests differ")
        if any(m.model_domain != self.model_domain for m in self.manifests):
            raise ValueError("multipart model domain differs")


class MultipartKVCacheStore:
    """Memory-only multipart adapter. Manifests are immutable and hard pinned."""

    def __init__(
        self, native: Any, manifest: KVCacheStoreManifest, *, cache_capacity: int = 64
    ) -> None:
        require_integer(cache_capacity, "cache_capacity", minimum=1)
        self.backend = (
            native if isinstance(native, MultipartBackend) else MultipartBackend(native)
        )
        self.manifest = manifest
        self.planning_limits = DEFAULT_STORE_PLANNING_LIMITS
        self.transfer_limits = DEFAULT_TRANSFER_LIMITS
        self.cache_capacity = cache_capacity
        self._manifests: OrderedDict[str, KVCacheStoreManifest] = OrderedDict()
        self._lock = RLock()

    def register_layout(self) -> None:
        self.backend.publish_manifest(
            self.manifest.manifest_key,
            kv_cache_store_manifest_to_json(self.manifest).encode(),
        )

    def prepare_page_reader(
        self,
        placement: KVCachePlacementManifest,
        binding: KVCacheResolvedRuntimeBinding,
    ) -> MultipartPageReader:
        return MultipartPageReader(self, placement, binding)

    def discover(
        self,
        page_keys: Sequence[str],
        *,
        operation_id: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> MultipartReadContext:
        page_keys = require_manifest_items(page_keys, "page_keys", str)
        pages, ids, selected = [], [], {}
        if cancelled is not None and cancelled():
            return MultipartReadContext(
                operation_id, self.manifest.model_domain, (), (), ()
            )
        # A key directly names the object and its source manifest. No directory
        # scan, source-layout ranking, or cross-key completeness probe.
        for start in range(0, len(page_keys), 128):
            batch = page_keys[start : start + 128]
            responses = self.backend.store.batch_query_parts(batch)
            if len(responses) != len(batch):
                raise KVCacheStoreError("multipart query result count differs")
            for page, (status, ref, count) in zip(batch, responses):
                if status != 0:
                    return MultipartReadContext(
                        operation_id,
                        self.manifest.model_domain,
                        tuple(pages),
                        tuple(ids),
                        tuple(selected.values()),
                    )
                with self._lock:
                    manifest = self._manifests.get(ref)
                    if manifest is None:
                        manifest = kv_cache_store_manifest_from_json(
                            self.backend.get_manifest(ref).decode()
                        )
                        if (
                            manifest.manifest_key != ref
                            or manifest.model_domain != self.manifest.model_domain
                            or len(manifest.layout.shards) != count
                        ):
                            raise KVCacheStoreError(
                                "object manifest identity or part count differs"
                            )
                        self._manifests[ref] = manifest
                    self._manifests.move_to_end(ref)
                    while len(self._manifests) > self.cache_capacity:
                        self._manifests.popitem(last=False)
                if len(manifest.layout.shards) != count:
                    raise KVCacheStoreError("cached manifest part count differs")
                pages.append(page)
                ids.append(manifest.layout.layout_id)
                selected[manifest.layout.layout_id] = manifest
        return MultipartReadContext(
            operation_id,
            self.manifest.model_domain,
            tuple(pages),
            tuple(ids),
            tuple(selected.values()),
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
        if plan.layout != self.manifest.layout:
            raise ValueError("multipart upload layout differs")
        if cancelled is not None and cancelled():
            return (False,) * len(page_keys)
        owned = [
            i
            for i, writer in enumerate(plan.part_writers)
            if writer == binding.participant_id
        ]
        if len(owned) != 1:
            raise ValueError("multipart demo requires one K/V shard per writer")
        fragments = tuple(f for f in plan.layout.fragments if f.part_index in owned)
        records = lower_store_ranges(
            plan.source_placement,
            binding,
            (fragments,) * len(page_keys),
            operation_id,
            self.transfer_limits,
        )
        regions = {r.region_id: r for r in binding.regions}
        grouped = [[] for _ in page_keys]
        for record in records:
            grouped[record.page_index].append(record)
        pointers, lengths = [], []
        for row in grouped:
            row.sort(key=lambda r: (r.part_index, r.part_offset))
            for obj in owned:
                cursor = 0
                for record in row:
                    if record.part_index == obj:
                        if record.part_offset != cursor:
                            raise ValueError("multipart upload byte gap")
                        cursor += record.nbytes
                if cursor != plan.layout.part_sizes[obj]:
                    raise ValueError("multipart upload size differs")
            pointers.append(
                [regions[r.region_id].address + r.region_offset for r in row]
            )
            lengths.append([r.nbytes for r in row])
        results = self.backend.store.batch_put_parts_from(
            page_keys,
            owned[0],
            len(plan.layout.shards),
            self.manifest.manifest_key,
            pointers,
            lengths,
        )
        return tuple(status == 0 for status in results)
