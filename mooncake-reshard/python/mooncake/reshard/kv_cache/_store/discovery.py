"""Bounded key probes and per-page source selection; no Store RPCs or cache state."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..types import require_manifest_items, require_nonempty_string
from .catalog import KVCacheStoreLayoutCatalog
from .manifest import _layout_digest
from .planner import DEFAULT_STORE_PLANNING_LIMITS, KVCacheStorePlanningLimits


@dataclass(frozen=True, init=False)
class KVCacheStoreProbePlan:
    """Keys ordered by page, canonical catalog entry, shard, then K/V."""

    catalog: KVCacheStoreLayoutCatalog
    page_keys: tuple[str, ...]
    keys: tuple[str, ...]

    def __init__(
        self,
        catalog: KVCacheStoreLayoutCatalog,
        page_keys: Sequence[str],
        *,
        limits: KVCacheStorePlanningLimits = DEFAULT_STORE_PLANNING_LIMITS,
    ) -> None:
        if not isinstance(catalog, KVCacheStoreLayoutCatalog):
            raise ValueError("catalog must be a KVCacheStoreLayoutCatalog")  # noqa: TRY004
        if not isinstance(limits, KVCacheStorePlanningLimits):
            raise ValueError("limits must be KVCacheStorePlanningLimits")  # noqa: TRY004
        pages = require_manifest_items(page_keys, "page_keys", str)
        for page in pages:
            require_nonempty_string(page, "page_key")
        objects_per_page = sum(2 * len(entry.key_suffixes) for entry in catalog.entries)
        if len(pages) * objects_per_page > limits.max_probe_keys:
            raise ValueError("probe key count limit exceeded")
        # Measure suffix bytes once with a one-byte placeholder, before N-page expansion.
        suffix_bytes = sum(
            len(key.encode("utf-8")) - 1
            for entry in catalog.entries
            for key in entry.object_keys("x")
        )
        byte_count = objects_per_page * sum(len(p.encode("utf-8")) for p in pages)
        byte_count += len(pages) * suffix_bytes
        if byte_count > limits.max_probe_bytes:
            raise ValueError("probe key byte limit exceeded")
        object.__setattr__(self, "catalog", catalog)
        object.__setattr__(self, "page_keys", pages)
        object.__setattr__(
            self,
            "keys",
            tuple(
                key
                for page in pages
                for entry in catalog.entries
                for key in entry.object_keys(page)
            ),
        )


def plan_kv_cache_store_probe(
    catalog: KVCacheStoreLayoutCatalog,
    page_keys: Sequence[str],
    *,
    limits: KVCacheStorePlanningLimits = DEFAULT_STORE_PLANNING_LIMITS,
) -> KVCacheStoreProbePlan:
    """Prepare raw batch_is_exist inputs without fetching any manifest."""
    return KVCacheStoreProbePlan(catalog, page_keys, limits=limits)


def select_kv_cache_store_sources(
    plan: KVCacheStoreProbePlan,
    exists_results: Sequence[int],
    *,
    preferred_layout_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Return one complete layout per page of the contiguous hit prefix.

    Native 1 means readable, 0/negative means miss/error. Preferences only order
    candidates; they never bypass fresh existence results or fill partial layouts.
    """
    if not isinstance(plan, KVCacheStoreProbePlan):
        raise ValueError("plan must be a KVCacheStoreProbePlan")  # noqa: TRY004
    results = require_manifest_items(exists_results, "exists_results", int)
    if len(results) != len(plan.keys):
        raise ValueError("existence result count differs from probe keys")
    if any(type(value) is not int or value > 1 for value in results):
        raise ValueError("existence results must be native integer statuses")
    preferred = require_manifest_items(
        preferred_layout_ids, "preferred_layout_ids", str
    )
    for layout_id in preferred:
        _layout_digest(layout_id)
    if len(set(preferred)) != len(preferred):
        raise ValueError("duplicate preferred layout ID")
    spans: dict[str, tuple[int, int]] = {}
    width = 0
    for entry in plan.catalog.entries:
        end = width + 2 * len(entry.key_suffixes)
        spans[entry.layout_id] = (width, end)
        width = end
    preferred_set = set(preferred)
    order = tuple(i for i in preferred if i in spans) + tuple(
        i for i in spans if i not in preferred_set
    )
    selected: list[str] = []
    for page_index in range(len(plan.page_keys)):
        base = page_index * width
        for layout_id in order:
            start, end = spans[layout_id]
            if all(value == 1 for value in results[base + start : base + end]):
                selected.append(layout_id)
                break
        else:
            break
    return tuple(selected)


__all__ = [
    "KVCacheStoreProbePlan",
    "plan_kv_cache_store_probe",
    "select_kv_cache_store_sources",
]
