"""Native multipart reads with self-contained manifest metadata."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from .manifest import KVCacheStoreManifest
from .serde import kv_cache_store_manifest_to_json

if TYPE_CHECKING:
    from .multipart import MultipartReadContext


class KVCacheStoreError(RuntimeError):
    """A native operation or metadata protocol failed."""


@lru_cache(maxsize=64)
def manifest_metadata(manifest: KVCacheStoreManifest) -> str:
    """Encode a cache identity and canonical JSON, without publishing an object.

    The native demo treats ObjectMetadata.manifest_key as opaque text.
    Cache serialization of immutable manifests for repeated read snapshots.
    """
    return manifest.manifest_key + "\n" + kv_cache_store_manifest_to_json(manifest)


class MultipartBackend:
    def __init__(self, store: Any) -> None:
        self.store = store
        for name in (
            "batch_put_parts_from",
            "batch_query_parts",
            "prepare_get_parts_snapshot",
            "prepare_get_into_ranges_template",
            "get_into_ranges_from_template",
        ):
            if not callable(getattr(store, name, None)):
                raise KVCacheStoreError(
                    f"native Store lacks multipart capability: {name}"
                )

    def ranged_reader(self, context: MultipartReadContext):
        manifests = {m.layout.layout_id: m for m in context.manifests}
        metadata = {key: manifest_metadata(m) for key, m in manifests.items()}
        sources = [manifests[i] for i in context.layout_ids]
        snapshot = None

        def read(*args, **kwargs):
            nonlocal snapshot
            if snapshot is None:
                snapshot = self.store.prepare_get_parts_snapshot(
                    context.page_keys,
                    [metadata[i] for i in context.layout_ids],
                    [len(m.layout.shards) for m in sources],
                )
            return self.store.get_into_ranges_from_template(snapshot, *args, **kwargs)

        return read
