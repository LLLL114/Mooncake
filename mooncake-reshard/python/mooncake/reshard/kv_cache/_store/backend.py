"""Native multipart reads and immutable manifest publication."""

from __future__ import annotations

import logging
import time
from importlib import import_module
from typing import Any

logger = logging.getLogger(__name__)


class KVCacheStoreError(RuntimeError):
    """A native operation or metadata protocol failed."""


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

    def metadata_config(self) -> Any:
        module = import_module("mooncake.store")
        config: Any = module.ReplicateConfig()
        config.data_type = module.ObjectDataType.METADATA
        config.with_hard_pin = True
        return config

    def get_manifest(self, key: str) -> bytes:
        value = self.store.get(key)
        if type(value) is not bytes or not value or len(value) > 16 * 1024 * 1024:
            raise KVCacheStoreError("manifest is missing or exceeds its size bound")
        return value

    def publish_manifest(self, key: str, value: bytes, attempts: int = 8) -> None:
        for attempt in range(attempts):
            try:
                self.store.put(key, value, self.metadata_config())
            except (RuntimeError, OSError) as error:
                logger.debug("Manifest put failed; checking publication: %s", error)
            try:
                observed = self.get_manifest(key)
            except KVCacheStoreError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(min(0.001 * 2**attempt, 0.02))
                continue
            if observed != value:
                raise KVCacheStoreError(
                    "published manifest differs from expected content"
                )
            return
