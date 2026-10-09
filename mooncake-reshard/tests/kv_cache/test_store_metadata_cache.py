"""Derived caches preserve wire identities and never cache payload availability."""

import pickle
from dataclasses import asdict, replace
from unittest.mock import patch

from mooncake.reshard.kv_cache import kv_cache_store_manifest_to_json
from mooncake.reshard.kv_cache.snapshot import _canonical_digest
from test_store_contracts import _manifest


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
