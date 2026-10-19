"""Native multipart transfer using explicitly supplied source manifests."""

from __future__ import annotations

from typing import Any


class KVCacheStoreError(RuntimeError):
    """A native operation or metadata protocol failed."""


class MultipartBackend:
    def __init__(self, store: Any) -> None:
        self.store = store
        for name in (
            "batch_put_parts_from",
            "prepare_get_parts_snapshot",
            "prepare_get_into_ranges_template",
            "get_into_ranges_from_template",
        ):
            if not callable(getattr(store, name, None)):
                raise KVCacheStoreError(
                    f"native Store lacks multipart capability: {name}"
                )

    def ranged_reader(self, context):
        manifests = {m.layout.layout_id: m for m in context.manifests}
        sources = [manifests[i] for i in context.layout_ids]
        snapshot = None

        def read(*args, **kwargs):
            nonlocal snapshot
            if snapshot is None:
                snapshot = self.store.prepare_get_parts_snapshot(
                    context.page_keys,
                    [m.manifest_key for m in sources],
                    [len(m.layout.shards) for m in sources],
                )
            return self.store.get_into_ranges_from_template(snapshot, *args, **kwargs)

        return read
