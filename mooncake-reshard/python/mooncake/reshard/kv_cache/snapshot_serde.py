"""Strict JSON boundary for KV-cache snapshot descriptors."""

from __future__ import annotations

from collections.abc import Mapping

from ..contracts import ResourceId, ResourceKind
from ._wire import canonical_json, load_json_object
from ._wire import json_integer as _integer
from ._wire import json_string as _string
from .snapshot import KVCacheSnapshotDescriptor, SnapshotId


def kv_cache_snapshot_to_json(snapshot: KVCacheSnapshotDescriptor) -> str:
    if not isinstance(snapshot, KVCacheSnapshotDescriptor):
        raise ValueError("snapshot must be a KVCacheSnapshotDescriptor")  # noqa: TRY004
    return canonical_json(
        {
            "resource_kind": snapshot.resource_kind.value,
            "namespace": snapshot.namespace,
            "resource_id": snapshot.resource_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.digest,
            "model_id": snapshot.model_id,
            "model_revision": snapshot.model_revision,
            "token_start": snapshot.token_start,
            "token_count": snapshot.token_count,
            "token_fingerprint": snapshot.token_fingerprint,
            "semantic_fingerprint": snapshot.semantic_fingerprint,
        },
    )


def kv_cache_snapshot_from_json(value: str) -> KVCacheSnapshotDescriptor:
    payload = _load_json_object(value)
    expected = {
        "resource_kind",
        "namespace",
        "resource_id",
        "snapshot_id",
        "snapshot_digest",
        "model_id",
        "model_revision",
        "token_start",
        "token_count",
        "token_fingerprint",
        "semantic_fingerprint",
    }
    if set(payload) != expected:
        raise ValueError("KV-cache snapshot fields do not match contract")
    if (
        _string(payload["resource_kind"], "resource_kind")
        != ResourceKind.KV_CACHE.value
    ):
        raise ValueError("resource_kind must be kv_cache")
    snapshot = KVCacheSnapshotDescriptor(
        namespace=_string(payload["namespace"], "namespace"),
        resource_id=ResourceId(_string(payload["resource_id"], "resource_id")),
        snapshot_id=SnapshotId(_string(payload["snapshot_id"], "snapshot_id")),
        model_id=_string(payload["model_id"], "model_id"),
        model_revision=_string(payload["model_revision"], "model_revision"),
        token_start=_integer(payload["token_start"], "token_start"),
        token_count=_integer(payload["token_count"], "token_count", minimum=1),
        token_fingerprint=_string(payload["token_fingerprint"], "token_fingerprint"),
        semantic_fingerprint=_string(
            payload["semantic_fingerprint"], "semantic_fingerprint"
        ),
    )
    if snapshot.digest != _string(payload["snapshot_digest"], "snapshot_digest"):
        raise ValueError("KV-cache snapshot digest does not match content")
    return snapshot


def _load_json_object(value: str) -> Mapping[str, object]:
    return load_json_object(value, "KV-cache snapshot")


__all__ = ["kv_cache_snapshot_from_json", "kv_cache_snapshot_to_json"]
