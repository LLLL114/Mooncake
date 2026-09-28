"""Synchronous native Store boundary; metadata updates never use blind upsert."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from importlib import import_module
from typing import Any, cast

logger = logging.getLogger(__name__)


class KVCacheStoreError(RuntimeError):
    """A native operation or metadata protocol failed."""


class StoreBackend:
    def __init__(self, store: Any) -> None:
        self.store = store
        for name in (
            "read_metadata_for_update",
            "compare_exchange_metadata",
            "get",
            "put",
            "batch_is_exist",
            "batch_get_buffer",
            "batch_put_from_multi_buffers",
            "get_into_ranges",
        ):
            if not callable(getattr(store, name, None)):
                raise KVCacheStoreError(
                    f"native Store lacks required capability: {name}"
                )

    def ranged_reader(
        self, keys: Iterable[str], *, templates: bool = False, plans: bool = False
    ) -> Callable[..., Any]:
        """Reuse one metadata snapshot within a load, prepared on its first read.

        Whole-object fast paths and cancelled loads do not query metadata here.
        The native Python binding refreshes leases before subsequent batches.
        A new load gets a new reader; snapshots never outlive their source set.
        """
        prepare = getattr(self.store, "prepare_get_into_ranges_snapshot", None)
        read = getattr(
            self.store,
            (
                "get_into_ranges_from_plan"
                if plans
                else "get_into_ranges_from_template"
                if templates
                else "get_into_ranges_from_snapshot"
            ),
            None,
        )
        if prepare is None and read is None and not templates and not plans:
            return cast(
                Callable[..., list[list[list[int]]]], self.store.get_into_ranges
            )
        if not callable(prepare) or not callable(read):
            raise KVCacheStoreError(
                "native Store must expose both ranged-read snapshot methods"
            )
        snapshot: object | None = None

        def read_ranges(*args: Any, **kwargs: Any) -> Any:
            nonlocal snapshot
            if snapshot is None:
                snapshot = prepare(list(dict.fromkeys(keys)))
                if snapshot is None:
                    raise KVCacheStoreError("native Store returned an invalid snapshot")
            return read(snapshot, *args, **kwargs)

        return read_ranges

    def metadata_config(self) -> Any:
        module = import_module("mooncake.store")
        config: Any = module.ReplicateConfig()
        config.data_type = module.ObjectDataType.METADATA
        config.with_hard_pin = True
        return config

    def payload_config(self) -> Any:
        module = import_module("mooncake.store")
        config: Any = module.ReplicateConfig()
        config.data_type = module.ObjectDataType.KVCACHE
        return config

    def read_catalog(self, key: str) -> tuple[bytes | None, str]:
        result: object = self.store.read_metadata_for_update(key)
        if not isinstance(result, tuple) or len(cast(tuple[object, ...], result)) != 3:
            raise KVCacheStoreError("invalid metadata read response")
        status, value, token = cast(tuple[object, object, object], result)
        if type(status) is not int or status not in (0, 1):
            raise KVCacheStoreError(f"metadata read failed: {status}")
        if type(value) is not bytes or type(token) is not str:
            raise KVCacheStoreError("invalid metadata read value/token")
        if status == 0:
            if value or token:
                raise KVCacheStoreError("absent metadata returned content")
            return None, ""
        if not value or not token or len(value) > 16 * 1024 * 1024:
            raise KVCacheStoreError("invalid metadata value size/token")
        return value, token

    def compare_catalog(self, key: str, token: str, value: bytes) -> bool:
        status = self.store.compare_exchange_metadata(key, token, value)
        if type(status) is not int or status not in (0, 1):
            raise KVCacheStoreError(f"metadata compare-exchange failed: {status}")
        return status == 1

    def get_manifest(self, key: str) -> bytes:
        value = self.store.get(key)
        if type(value) is not bytes or not value or len(value) > 16 * 1024 * 1024:
            raise KVCacheStoreError("manifest is missing or exceeds its size bound")
        return value

    def get_manifests(self, keys: list[str]) -> list[bytes]:
        handles = self.store.batch_get_buffer(keys)
        if len(handles) != len(keys):
            raise KVCacheStoreError("manifest batch result count differs")
        values: list[bytes] = []
        for handle in handles:
            if handle is None:
                raise KVCacheStoreError("manifest is missing")
            with memoryview(handle) as view:
                if not 0 < view.nbytes <= 16 * 1024 * 1024:
                    raise KVCacheStoreError("manifest exceeds its size bound")
                values.append(view.tobytes())
        return values

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
