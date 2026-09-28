"""Small model layout directories; registration here is pure metadata, never I/O."""

from __future__ import annotations

from dataclasses import dataclass

from ..types import require_manifest_items, require_nonempty_string, require_sha256
from .manifest import KVCacheStoreManifest, _layout_digest, _manifest_key, _object_keys


@dataclass(frozen=True)
class KVCacheStoreLayoutEntry:
    """Enough information to probe a layout before its full manifest is cached."""

    layout_id: str
    key_suffixes: tuple[str, ...]

    def __post_init__(self) -> None:
        _layout_digest(self.layout_id)
        suffixes = require_manifest_items(self.key_suffixes, "key_suffixes", str)
        if not suffixes or 2 * len(suffixes) > 100_000:
            raise ValueError("directory entry object count limit exceeded or empty")
        for suffix in suffixes:
            require_nonempty_string(suffix, "key_suffix")
        if len(set(suffixes)) != len(suffixes):
            raise ValueError("directory entry key suffixes must be unique")
        object.__setattr__(self, "key_suffixes", suffixes)

    @classmethod
    def from_manifest(cls, manifest: KVCacheStoreManifest) -> KVCacheStoreLayoutEntry:
        """Preserve shard order so probe keys and manifest object indices agree."""
        if not isinstance(manifest, KVCacheStoreManifest):
            raise ValueError("manifest must be a KVCacheStoreManifest")  # noqa: TRY004
        return cls(
            manifest.layout.layout_id,
            tuple(s.key_suffix for s in manifest.layout.shards),
        )

    def manifest_key(self, model_domain: str) -> str:
        return _manifest_key(model_domain, self.layout_id)

    def object_keys(self, page_key: str) -> tuple[str, ...]:
        return _object_keys(page_key, self.layout_id, self.key_suffixes)


@dataclass(frozen=True)
class KVCacheStoreLayoutCatalog:
    """One model's candidate layouts, not a statement of page availability."""

    model_domain: str
    entries: tuple[KVCacheStoreLayoutEntry, ...] = ()

    def __post_init__(self) -> None:
        require_sha256(self.model_domain, "model_domain")
        entries = require_manifest_items(
            self.entries, "catalog entries", KVCacheStoreLayoutEntry
        )
        if sum(2 * len(e.key_suffixes) for e in entries) > 100_000:
            raise ValueError("catalog object count limit exceeded")
        by_id: dict[str, KVCacheStoreLayoutEntry] = {}
        for entry in entries:
            if entry.layout_id in by_id and by_id[entry.layout_id] != entry:
                raise ValueError("conflicting directory entries for one layout")
            by_id[entry.layout_id] = entry
        object.__setattr__(self, "entries", tuple(by_id[key] for key in sorted(by_id)))

    @property
    def catalog_key(self) -> str:
        return f"kv/{self.model_domain}/layouts"

    def with_manifest(
        self, manifest: KVCacheStoreManifest
    ) -> KVCacheStoreLayoutCatalog:
        """Return an idempotent local merge; this is not an atomic Store update."""
        entry = KVCacheStoreLayoutEntry.from_manifest(manifest)
        if manifest.model_domain != self.model_domain:
            raise ValueError("manifest belongs to a different model domain")
        for current in self.entries:
            if current.layout_id == entry.layout_id:
                if current != entry:
                    raise ValueError("conflicting directory entries for one layout")
                return self
        return KVCacheStoreLayoutCatalog(self.model_domain, (*self.entries, entry))

    def validate_manifest(self, manifest: KVCacheStoreManifest) -> None:
        """Validate a fetched/cached manifest before consuming its byte mapping."""
        expected = KVCacheStoreLayoutEntry.from_manifest(manifest)
        if manifest.model_domain != self.model_domain:
            raise ValueError("manifest belongs to a different model domain")
        for entry in self.entries:
            if entry.layout_id == expected.layout_id:
                if entry != expected:
                    raise ValueError(
                        "manifest key suffixes differ from directory entry"
                    )
                return
        raise ValueError("manifest layout is absent from this directory")


__all__ = ["KVCacheStoreLayoutCatalog", "KVCacheStoreLayoutEntry"]
