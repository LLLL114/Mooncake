"""Logical shard views shared by live placement and durable Store manifests.

Parents validate their own fields and collection budgets before constructing
these views. Participant/rank identity and Store object names stay in the parent
contracts; coverage only needs layer/head geometry and explicit replica facts.
"""

from __future__ import annotations

from typing import NamedTuple

from ._head_index import HeadIntervalIndex
from .types import KVCacheDescriptor


class KVCacheLogicalShard(NamedTuple):
    layer_ids: tuple[int, ...]
    head_start: int
    head_count: int
    replica_ordinal: int = 0
    replica_count: int = 1


def validate_logical_coverage(
    descriptor: KVCacheDescriptor,
    shards: tuple[KVCacheLogicalShard, ...],
    *,
    label: str,
) -> None:
    """Prove one complete logical copy, allowing only exact declared replicas."""
    layers: dict[int, list[KVCacheLogicalShard]] = {
        layer: [] for layer in descriptor.global_layer_ids
    }
    for shard in shards:
        end = shard.head_start + shard.head_count
        if end > descriptor.total_kv_heads:
            raise ValueError(f"{label} head interval exceeds total_kv_heads")
        for layer in shard.layer_ids:
            if layer not in layers:
                raise ValueError(f"{label} contains an unknown global layer")
            layers[layer].append(shard)
    for layer, items in layers.items():
        try:
            index = HeadIntervalIndex(
                (s.head_start, s.head_start + s.head_count, s) for s in items
            )
            for _, _, owners in index.cover(0, descriptor.total_kv_heads):
                if len(owners) == 1:
                    if owners[0].replica_count != 1:
                        raise ValueError("incomplete declared KV-head replica set")
                elif {s.replica_count for s in owners} != {len(owners)} or {
                    s.replica_ordinal for s in owners
                } != set(range(len(owners))):
                    raise ValueError(
                        "overlapping KV heads must be an exact declared replica set"
                    )
        except ValueError as error:
            raise ValueError(f"{label} layer={layer}: {error}") from error
