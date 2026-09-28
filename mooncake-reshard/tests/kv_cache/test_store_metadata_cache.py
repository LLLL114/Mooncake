"""Derived caches preserve wire identities and never cache payload availability."""

import pickle
from dataclasses import asdict, replace
from unittest.mock import patch

from mooncake.reshard.kv_cache import KVCacheStore, kv_cache_store_manifest_to_json
from mooncake.reshard.kv_cache._store import store as store_module
from mooncake.reshard.kv_cache.snapshot import _canonical_digest
from test_store_contracts import _manifest
from test_store_execution import MemoryBackend, MemoryNative


def test_derived_caches_preserve_canonical_identity_and_pickle():
    manifest = _manifest()
    wire = kv_cache_store_manifest_to_json(manifest)
    layout_digest = _canonical_digest(asdict(manifest.layout))
    manifest_digest = _canonical_digest(asdict(manifest))
    with patch(
        "mooncake.reshard.kv_cache._store.manifest._canonical_digest",
        wraps=_canonical_digest,
    ) as digest:
        for _ in range(10):
            assert manifest.layout.digest == layout_digest
            assert manifest.digest == manifest_digest
            assert manifest.model_domain
        assert digest.call_count == 0
    assert kv_cache_store_manifest_to_json(manifest) == wire
    restored = pickle.loads(pickle.dumps(manifest))
    assert restored == manifest and restored.digest == manifest_digest
    other = replace(manifest, semantic_fingerprint="different")
    assert other.model_domain != manifest.model_domain
    assert other.digest != manifest.digest


def test_catalog_parse_cache_observes_versions_and_eviction():
    native = MemoryNative()
    manifest = _manifest()
    store = KVCacheStore(MemoryBackend(native), manifest)
    store.register_layout()
    for key in manifest.object_keys("page"):
        native.values[key] = b"payload"
    with patch.object(
        store_module,
        "kv_cache_store_catalog_from_json",
        wraps=store_module.kv_cache_store_catalog_from_json,
    ) as parse:
        for _ in range(3):
            assert store.discover(("page",), operation_id="get").page_keys == ("page",)
        assert parse.call_count == 1
        del native.values[manifest.object_keys("page")[0]]
        assert not store.discover(("page",), operation_id="get").page_keys
        assert parse.call_count == 1
        native.tokens[store._catalog_cache[2].catalog_key] = "new-incarnation"
        assert not store.discover(("page",), operation_id="get").page_keys
        assert parse.call_count == 2
