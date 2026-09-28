"""Directory consistency, cold key probes and complete per-page source selection."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from mooncake.reshard.kv_cache import (
    KVCacheStoreLayoutCatalog,
    KVCacheStoreLayoutEntry,
    KVCacheStorePlanningLimits,
    kv_cache_store_catalog_from_json,
    kv_cache_store_catalog_to_json,
    plan_kv_cache_store_probe,
    select_kv_cache_store_sources,
)
from test_store_contracts import _layout, _manifest


def _sources():
    a = _manifest(_layout(1))
    b = _manifest(_layout(2, ((0, 1, 2, 3),)))
    catalog = (
        KVCacheStoreLayoutCatalog(a.model_domain).with_manifest(a).with_manifest(b)
    )
    return a, b, catalog


def _statuses(plan, available):
    return [1 if key in available else 0 for key in plan.keys]


def test_catalog_merge_is_model_scoped_deterministic_and_idempotent():
    a, b, catalog = _sources()
    assert catalog.catalog_key == f"kv/{a.model_domain}/layouts"
    reverse = (
        KVCacheStoreLayoutCatalog(a.model_domain).with_manifest(b).with_manifest(a)
    )
    assert catalog == reverse == catalog.with_manifest(a)
    assert tuple(e.layout_id for e in catalog.entries) == tuple(
        sorted(e.layout_id for e in catalog.entries)
    )
    catalog.validate_manifest(a)
    catalog.validate_manifest(b)
    entry = KVCacheStoreLayoutEntry.from_manifest(a)
    assert entry.manifest_key(a.model_domain) == a.manifest_key
    with pytest.raises(ValueError, match="different model"):
        catalog.with_manifest(replace(a, model_revision="different"))
    with pytest.raises(ValueError, match="different model"):
        catalog.validate_manifest(replace(a, namespace="different"))
    with pytest.raises(ValueError, match="absent"):
        KVCacheStoreLayoutCatalog(a.model_domain).validate_manifest(a)


def test_conflicting_entry_and_fetched_manifest_are_rejected():
    a, _, catalog = _sources()
    entry = KVCacheStoreLayoutEntry.from_manifest(a)
    changed = replace(entry, key_suffixes=tuple(reversed(entry.key_suffixes)))
    with pytest.raises(ValueError, match="conflicting"):
        KVCacheStoreLayoutCatalog(a.model_domain, (entry, changed))
    stale = KVCacheStoreLayoutCatalog(a.model_domain, (changed,))
    with pytest.raises(ValueError, match="suffixes differ"):
        stale.validate_manifest(a)
    with pytest.raises(ValueError, match="conflicting"):
        stale.with_manifest(a)
    assert catalog.with_manifest(a) == catalog


@pytest.mark.parametrize(
    "layout_id,suffixes",
    [
        ("bad", ("0",)),
        ("sha256:" + "X" * 64, ("0",)),
        ("sha256:" + "a" * 64, ()),
        ("sha256:" + "a" * 64, ("",)),
        ("sha256:" + "a" * 64, ("0", "0")),
    ],
)
def test_invalid_directory_entry(layout_id, suffixes):
    with pytest.raises(ValueError):
        KVCacheStoreLayoutEntry(layout_id, suffixes)


def test_cold_catalog_roundtrip_generates_exact_manifest_keys():
    a, b, catalog = _sources()
    wire = kv_cache_store_catalog_to_json(catalog)
    assert set(json.loads(wire)) == {"model_domain", "entries"}
    restored = kv_cache_store_catalog_from_json(wire)
    assert restored == catalog
    plan = plan_kv_cache_store_probe(restored, ("tag_page0", "tag_page1"))
    by_id = {m.layout.layout_id: m for m in (a, b)}
    expected = tuple(
        key
        for page in plan.page_keys
        for entry in restored.entries
        for key in by_id[entry.layout_id].object_keys(page)
    )
    assert plan.keys == expected
    assert set(a.object_keys("same")).isdisjoint(b.object_keys("same"))
    for entry in restored.entries:
        assert (
            entry.manifest_key(catalog.model_domain)
            == by_id[entry.layout_id].manifest_key
        )


def test_each_page_chooses_its_complete_layout_and_stops_at_first_hole():
    a, b, catalog = _sources()
    plan = plan_kv_cache_store_probe(catalog, ("p0", "p1", "p2", "p3"))
    available = (
        set(a.object_keys("p0")) | set(b.object_keys("p1")) | set(a.object_keys("p3"))
    )
    assert select_kv_cache_store_sources(plan, _statuses(plan, available)) == (
        a.layout.layout_id,
        b.layout.layout_id,
    )


def test_partial_layouts_are_not_stitched_and_negative_errors_are_not_hits():
    a, b, catalog = _sources()
    plan = plan_kv_cache_store_probe(catalog, ("p",))
    partial = set(a.object_keys("p")[::2]) | set(b.object_keys("p")[1::2])
    assert select_kv_cache_store_sources(plan, _statuses(plan, partial)) == ()
    available = set(a.object_keys("p")[1:]) | set(b.object_keys("p"))
    results = _statuses(plan, available)
    results[plan.keys.index(a.object_keys("p")[0])] = -7
    assert select_kv_cache_store_sources(
        plan, results, preferred_layout_ids=(a.layout.layout_id,)
    ) == (b.layout.layout_id,)


def test_fresh_probes_override_old_preferences_and_multiple_hits_are_deterministic():
    a, b, catalog = _sources()
    plan = plan_kv_cache_store_probe(catalog, ("p",))
    all_present = [1] * len(plan.keys)
    assert select_kv_cache_store_sources(plan, all_present) == (
        catalog.entries[0].layout_id,
    )
    assert select_kv_cache_store_sources(
        plan, all_present, preferred_layout_ids=(b.layout.layout_id,)
    ) == (b.layout.layout_id,)
    assert select_kv_cache_store_sources(
        plan,
        _statuses(plan, set(a.object_keys("p"))),
        preferred_layout_ids=(b.layout.layout_id,),
    ) == (a.layout.layout_id,)
    # A stale but well-formed preference is only a hint.
    assert select_kv_cache_store_sources(
        plan, all_present, preferred_layout_ids=("sha256:" + "0" * 64,)
    ) == (catalog.entries[0].layout_id,)


@pytest.mark.parametrize("value", [True, False, 2, 1.0, "1"])
def test_non_native_status_is_rejected(value):
    _, _, catalog = _sources()
    plan = plan_kv_cache_store_probe(catalog, ("p",))
    results = [1] * len(plan.keys)
    results[-1] = value
    with pytest.raises(ValueError):
        select_kv_cache_store_sources(plan, results)


def test_result_length_and_preference_validation():
    a, _, catalog = _sources()
    plan = plan_kv_cache_store_probe(catalog, ("p",))
    with pytest.raises(ValueError, match="count"):
        select_kv_cache_store_sources(plan, [1])
    for preferred in ((a.layout.layout_id, a.layout.layout_id), ("bad",)):
        with pytest.raises(ValueError):
            select_kv_cache_store_sources(
                plan, [1] * len(plan.keys), preferred_layout_ids=preferred
            )


def test_empty_catalog_empty_request_and_duplicate_pages():
    a, _, catalog = _sources()
    for c, pages in (
        (catalog, ()),
        (KVCacheStoreLayoutCatalog(a.model_domain), ("p",)),
    ):
        plan = plan_kv_cache_store_probe(c, pages)
        assert plan.keys == ()
        assert select_kv_cache_store_sources(plan, ()) == ()
    plan = plan_kv_cache_store_probe(catalog, ("p", "p"))
    selected = select_kv_cache_store_sources(
        plan, _statuses(plan, set(a.object_keys("p")))
    )
    assert selected == (a.layout.layout_id, a.layout.layout_id)


def test_probe_count_and_exact_utf8_byte_limits():
    _, _, catalog = _sources()
    pages = ("model_page0", "模型_page1")
    plan = plan_kv_cache_store_probe(catalog, pages)
    size = sum(len(key.encode("utf-8")) for key in plan.keys)
    limits = KVCacheStorePlanningLimits(
        max_probe_keys=len(plan.keys), max_probe_bytes=size
    )
    assert plan_kv_cache_store_probe(catalog, pages, limits=limits).keys == plan.keys
    for bad in (
        replace(limits, max_probe_keys=len(plan.keys) - 1),
        replace(limits, max_probe_bytes=size - 1),
    ):
        with pytest.raises(ValueError, match="probe key"):
            plan_kv_cache_store_probe(catalog, pages, limits=bad)
    with pytest.raises(ValueError):
        plan_kv_cache_store_probe(catalog, ("",))


def test_catalog_wire_and_aggregate_bounds():
    a, _, catalog = _sources()
    wire = json.loads(kv_cache_store_catalog_to_json(catalog))
    for field, value in (("extra", 1), ("model_domain", "bad"), ("entries", {})):
        changed = dict(wire, **{field: value})
        with pytest.raises(ValueError):
            kv_cache_store_catalog_from_json(json.dumps(changed))
    entry = {
        "layout_id": a.layout.layout_id,
        "key_suffixes": [str(i) for i in range(50001)],
    }
    with pytest.raises(ValueError, match="object count"):
        kv_cache_store_catalog_from_json(
            json.dumps({"model_domain": a.model_domain, "entries": [entry]})
        )
    with pytest.raises(ValueError):
        kv_cache_store_catalog_from_json('{"entries":[],"entries":[]}')


def test_idempotent_merge_at_catalog_capacity():
    a, _, _ = _sources()
    own = KVCacheStoreLayoutEntry.from_manifest(a)
    remaining = 50000 - len(own.key_suffixes)
    other = KVCacheStoreLayoutEntry(
        "sha256:" + "0" * 64, tuple(str(i) for i in range(remaining))
    )
    catalog = KVCacheStoreLayoutCatalog(a.model_domain, (own, other))
    assert catalog.with_manifest(a) == catalog


def test_probe_offsets_support_different_layout_object_counts():
    a = _manifest(
        _layout(1, (tuple(range(4)), tuple(range(4, 8)), tuple(range(8, 12))))
    )
    b = _manifest(_layout(2, (tuple(range(6)), tuple(range(6, 12)))))
    catalog = (
        KVCacheStoreLayoutCatalog(a.model_domain).with_manifest(a).with_manifest(b)
    )
    plan = plan_kv_cache_store_probe(catalog, ("p0", "p1", "p2"))
    assert len(a.object_keys("p0")) == 6
    assert len(b.object_keys("p0")) == 8
    assert len(plan.keys) == 3 * (6 + 8)
    available = set(a.object_keys("p0")) | set(b.object_keys("p1"))
    available |= set(a.object_keys("p2")[:-1]) | set(b.object_keys("p2"))
    assert select_kv_cache_store_sources(
        plan, _statuses(plan, available), preferred_layout_ids=(a.layout.layout_id,)
    ) == (a.layout.layout_id, b.layout.layout_id, b.layout.layout_id)
