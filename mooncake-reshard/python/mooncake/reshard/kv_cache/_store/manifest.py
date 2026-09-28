"""Shared, address-free KV Store layout and model metadata; no page snapshots."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from ..snapshot import _canonical_digest
from ..types import (
    KVCacheComponent,
    KVCacheDescriptor,
    require_integer,
    require_manifest_items,
    require_nonempty_string,
    require_sha256,
)


class KVCacheStoreFormat(str, Enum):
    """Axis order of one K or V object; D is always contiguous."""

    PLHD = "PLHD"  # [token, layer, head, dim]
    LPHD = "LPHD"  # [layer, token, head, dim]
    HPLD = "HPLD"  # [head, token, layer, dim]


@dataclass(frozen=True)
class KVCacheStoreShard:
    """One stable Store slot, with separate K and V objects for every page."""

    layer_ids: tuple[int, ...]
    head_start: int
    head_count: int
    key_suffix: str

    def __post_init__(self) -> None:
        require_nonempty_string(self.key_suffix, "key_suffix")
        layers = require_manifest_items(self.layer_ids, "shard layers", int)
        if not layers or len(set(layers)) != len(layers):
            raise ValueError("shard layers must be nonempty and unique")
        for layer in layers:
            require_integer(layer, "global_layer_id")
        require_integer(self.head_start, "head_start")
        require_integer(self.head_count, "head_count", minimum=1)
        require_integer(self.head_start + self.head_count, "head_end")
        object.__setattr__(self, "layer_ids", tuple(sorted(layers)))


@dataclass(frozen=True)
class KVCacheStoreFragment:
    """Relative byte mapping for one layer/component and a head interval."""

    global_layer_id: int
    component: KVCacheComponent
    head_start: int
    head_count: int
    object_index: int
    object_offset: int
    token_stride_bytes: int
    head_stride_bytes: int

    def __post_init__(self) -> None:
        for name in ("global_layer_id", "head_start", "object_index", "object_offset"):
            require_integer(getattr(self, name), name)
        for name in ("head_count", "token_stride_bytes", "head_stride_bytes"):
            require_integer(getattr(self, name), name, minimum=1)
        require_integer(self.head_start + self.head_count, "head_end")
        if not isinstance(self.component, KVCacheComponent):
            raise ValueError("component must be a KVCacheComponent")  # noqa: TRY004


@dataclass(frozen=True)
class KVCacheStoreLayout:
    """A fixed-page object template independent of models, pages and workers.

    Persist only shard geometry and axis order. Sizes and strides are derived,
    so a manifest cannot give conflicting descriptions of the same bytes.
    """

    descriptor: KVCacheDescriptor
    object_format: KVCacheStoreFormat
    shards: tuple[KVCacheStoreShard, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, KVCacheDescriptor):
            raise ValueError("descriptor must be a KVCacheDescriptor")  # noqa: TRY004
        if not isinstance(self.object_format, KVCacheStoreFormat):
            raise ValueError("object_format must be a KVCacheStoreFormat")  # noqa: TRY004
        shards = require_manifest_items(self.shards, "Store shards", KVCacheStoreShard)
        if len({shard.key_suffix for shard in shards}) != len(shards):
            raise ValueError("Store shard key suffixes must be unique")
        if not shards or len(shards) * 2 > 100_000:
            raise ValueError("Store object count limit exceeded or empty layout")
        if sum(len(shard.layer_ids) * 2 for shard in shards) > 100_000:
            raise ValueError("Store fragment count limit exceeded")
        object.__setattr__(
            self,
            "shards",
            tuple(sorted(shards, key=lambda s: (s.layer_ids, s.head_start))),
        )
        _validate_layout_coverage(self)
        if sum(self.object_sizes) > 1 << 40:
            raise ValueError("Store layout byte limit exceeded")

    @property
    def token_count(self) -> int:
        """Every object contains exactly one full page, never a variable tail."""
        return self.descriptor.page_size

    @property
    def object_sizes(self) -> tuple[int, ...]:
        """Object 2*s is K and object 2*s+1 is V for Store slot s."""
        d = self.descriptor
        return tuple(
            require_integer(
                len(shard.layer_ids)
                * self.token_count
                * shard.head_count
                * dim
                * d.itemsize,
                "object size",
                minimum=1,
            )
            for shard in self.shards
            for dim in (d.key_head_dim, d.value_head_dim)
        )

    @property
    def fragments(self) -> tuple[KVCacheStoreFragment, ...]:
        """Derive affine offsets without enumerating tokens or individual bytes."""
        result: list[KVCacheStoreFragment] = []
        d = self.descriptor
        for slot, shard in enumerate(self.shards):
            layers, heads, tokens = (
                len(shard.layer_ids),
                shard.head_count,
                self.token_count,
            )
            for component_index, component in enumerate(KVCacheComponent):
                dim = (
                    d.key_head_dim
                    if component is KVCacheComponent.KEY
                    else d.value_head_dim
                )
                head_bytes = dim * d.itemsize
                if self.object_format is KVCacheStoreFormat.PLHD:
                    layer_stride, token_stride, head_stride = (
                        heads * head_bytes,
                        layers * heads * head_bytes,
                        head_bytes,
                    )
                elif self.object_format is KVCacheStoreFormat.LPHD:
                    layer_stride, token_stride, head_stride = (
                        tokens * heads * head_bytes,
                        heads * head_bytes,
                        head_bytes,
                    )
                else:
                    layer_stride, token_stride, head_stride = (
                        head_bytes,
                        layers * head_bytes,
                        tokens * layers * head_bytes,
                    )
                for local_layer, global_layer in enumerate(shard.layer_ids):
                    result.append(
                        KVCacheStoreFragment(
                            global_layer,
                            component,
                            shard.head_start,
                            heads,
                            2 * slot + component_index,
                            local_layer * layer_stride,
                            token_stride,
                            head_stride,
                        )
                    )
        return tuple(result)

    @property
    def digest(self) -> str:
        return _canonical_digest(asdict(self))

    @property
    def layout_id(self) -> str:
        return f"sha256:{self.digest}"


def _validate_layout_coverage(layout: KVCacheStoreLayout) -> None:
    """Prove each global layer/head occurs once; dense formats then cover bytes."""
    heads: dict[int, list[tuple[int, int]]] = {
        layer: [] for layer in layout.descriptor.global_layer_ids
    }
    for shard in layout.shards:
        end = shard.head_start + shard.head_count
        if end > layout.descriptor.total_kv_heads:
            raise ValueError("Store shard exceeds global heads")
        for layer in shard.layer_ids:
            if layer not in heads:
                raise ValueError("Store shard contains an unknown layer")
            heads[layer].append((shard.head_start, end))
    for intervals in heads.values():
        cursor = 0
        for start, end in sorted(intervals):
            if start != cursor:
                raise ValueError("logical heads coverage has a gap or overlap")
            cursor = end
        if cursor != layout.descriptor.total_kv_heads:
            raise ValueError("logical layer/head coverage is incomplete")


@dataclass(frozen=True)
class KVCacheStoreManifest:
    """One immutable layout record shared by pages stored in that layout.

    Constructing it does not publish a layout or prove any payload exists.
    Directory registration and object availability belong to Phase B.
    """

    namespace: str
    model_id: str
    model_revision: str
    semantic_fingerprint: str
    layout: KVCacheStoreLayout

    def __post_init__(self) -> None:
        for name in ("namespace", "model_id", "model_revision", "semantic_fingerprint"):
            require_nonempty_string(getattr(self, name), name)
        if not isinstance(self.layout, KVCacheStoreLayout):
            raise ValueError("layout must be a KVCacheStoreLayout")  # noqa: TRY004

    @property
    def model_domain(self) -> str:
        """Exclude TP/PP geometry so layouts share one discovery directory."""
        return _canonical_digest(
            {
                "namespace": self.namespace,
                "model_id": self.model_id,
                "model_revision": self.model_revision,
                "semantic_fingerprint": self.semantic_fingerprint,
                "descriptor": asdict(self.layout.descriptor),
                "object_format": self.layout.object_format,
            }
        )

    @property
    def manifest_key(self) -> str:
        return _manifest_key(self.model_domain, self.layout.layout_id)

    @property
    def digest(self) -> str:
        return _canonical_digest(asdict(self))

    def object_keys(self, page_key: str) -> tuple[str, ...]:
        """Keep the framework prefix, adding layout isolation and rank suffixes."""
        return _object_keys(
            page_key,
            self.layout.layout_id,
            tuple(shard.key_suffix for shard in self.layout.shards),
        )


def _layout_digest(layout_id: str) -> str:
    """Decode a canonical layout ID before using it in object keys."""
    require_nonempty_string(layout_id, "layout_id")
    if not layout_id.startswith("sha256:"):
        raise ValueError("layout_id must start with sha256:")
    return require_sha256(layout_id[7:], "layout digest")


def _manifest_key(model_domain: str, layout_id: str) -> str:
    """Shared by manifests and cold-cache directory entries."""
    require_sha256(model_domain, "model_domain")
    return f"kv/{model_domain}/manifests/{_layout_digest(layout_id)}"


def _object_keys(
    page_key: str,
    layout_id: str,
    key_suffixes: tuple[str, ...],
) -> tuple[str, ...]:
    """Use identical naming for cold probes and manifest-based reads."""
    require_nonempty_string(page_key, "page_key")
    digest = _layout_digest(layout_id)
    return tuple(
        f"{page_key}_l{digest}_{suffix}_{component}"
        for suffix in key_suffixes
        for component in ("k", "v")
    )


__all__ = [
    "KVCacheStoreFormat",
    "KVCacheStoreFragment",
    "KVCacheStoreLayout",
    "KVCacheStoreManifest",
    "KVCacheStoreShard",
]
