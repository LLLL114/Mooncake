"""Bounded lowering of complete runtime-to-runtime operations."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ._range_ops import index_runtime_ranges, plan_range_copy
from .planner import KVCacheLogicalTransferPlan, _build_transfer_edges
from .resolved import (
    KVCacheResolvedRuntimeBinding,
    KVCacheTransferLimits,
    _check_disjoint,
    _validated_spans,
)
from .snapshot import _canonical_digest
from .types import require_manifest_items, require_nonempty_string


@dataclass(frozen=True)
class KVCacheWrite:
    source_participant_id: str
    target_participant_id: str
    endpoint: str
    source_address: int
    target_address: int
    nbytes: int


@dataclass(frozen=True)
class KVCacheRuntimeTransferPlan:
    """One immutable operation covering every nonempty target participant.

    Physical writes are derived here, never accepted from a wire payload.
    Source bindings contain exactly the selected writers. Target bindings and
    logical plans contain every nonempty participant of the target placement.
    """

    operation_id: str
    logical_plans: tuple[KVCacheLogicalTransferPlan, ...]
    source_bindings: tuple[KVCacheResolvedRuntimeBinding, ...]
    target_bindings: tuple[KVCacheResolvedRuntimeBinding, ...]
    limits: KVCacheTransferLimits = field(default_factory=KVCacheTransferLimits)
    writes: tuple[KVCacheWrite, ...] = field(init=False)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        require_nonempty_string(self.operation_id, "operation_id")
        if not isinstance(self.limits, KVCacheTransferLimits):
            raise TypeError("limits must be KVCacheTransferLimits")
        plans = require_manifest_items(
            self.logical_plans, "logical plans", KVCacheLogicalTransferPlan
        )
        if not plans:
            raise ValueError("runtime operation must contain logical plans")
        # Revalidate every edge at the execution boundary, including in-process
        # callers. A writer-filtered plan cannot attest complete target coverage.
        plans = tuple(
            sorted((replace(p) for p in plans), key=lambda p: p.target_participant_id)
        )
        first = plans[0]
        if first.snapshot is None:
            raise ValueError("runtime execution requires an explicit snapshot")
        targets = tuple(p.target_participant_id for p in plans)
        expected_targets = tuple(
            sorted(
                p.participant_id for p in first.target_placement.parts if p.layer_ids
            )
        )
        if targets != expected_targets:
            raise ValueError(
                "runtime operation must cover every target participant exactly once"
            )
        for plan in plans:
            if (
                plan.source_placement.digest != first.source_placement.digest
                or plan.target_placement.digest != first.target_placement.digest
                or plan.snapshot != first.snapshot
            ):
                raise ValueError(
                    "runtime operation plans disagree on placement or snapshot"
                )
            if plan.edges != _build_transfer_edges(
                plan.source_placement, plan.target_part, plan.source_dp_rank
            ):
                raise ValueError(
                    "runtime execution requires complete local-target plans"
                )
        source_ids: set[str] = {
            writer for p in plans for writer in p.expected_writer_ids
        }
        sources = _bindings(self.source_bindings, source_ids, "source")
        bindings = _bindings(self.target_bindings, set(targets), "target")
        all_spans: dict[str, list[tuple[int, int]]] = {}
        span_count = 0
        endpoint_instances: dict[str, str] = {}
        instance_endpoints: dict[str, str] = {}
        for placement, items in (
            (first.source_placement, sources),
            (first.target_placement, bindings),
        ):
            for binding in items:
                if span_count >= self.limits.max_operations:
                    raise ValueError("runtime validation expansion limit exceeded")
                spans = _validated_spans(
                    placement,
                    first.snapshot,
                    binding,
                    self.operation_id,
                    replace(
                        self.limits,
                        max_operations=self.limits.max_operations - span_count,
                    ),
                )
                span_count += len(spans)
                endpoint = binding.regions[0].endpoint
                if (
                    endpoint_instances.setdefault(endpoint, binding.instance_id)
                    != binding.instance_id
                ):
                    raise ValueError(
                        "one endpoint cannot represent different runtime instances"
                    )
                if (
                    instance_endpoints.setdefault(binding.instance_id, endpoint)
                    != endpoint
                ):
                    raise ValueError("one runtime instance must use one endpoint")
                current = all_spans.setdefault(binding.instance_id, [])
                if len(current) + len(spans) > self.limits.max_operations:
                    raise ValueError(
                        "shared-instance validation expansion limit exceeded"
                    )
                current.extend(spans)
        for spans in all_spans.values():
            _check_disjoint(spans, "shared runtime instance")
        object.__setattr__(self, "logical_plans", plans)
        object.__setattr__(self, "source_bindings", sources)
        object.__setattr__(self, "target_bindings", bindings)
        object.__setattr__(
            self, "writes", _lower(plans, sources, bindings, self.limits)
        )
        object.__setattr__(
            self,
            "digest",
            _canonical_digest(
                {
                    "schema": "kv-cache-runtime-transfer",
                    "operation_id": self.operation_id,
                    "plans": [p.digest for p in plans],
                    "sources": [b.digest for b in sources],
                    "targets": [b.digest for b in bindings],
                }
            ),
        )

    @property
    def expected_writers(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                (writer, p.target_participant_id)
                for p in self.logical_plans
                for writer in p.expected_writer_ids
            )
        )

    def writer_bytes(self, writer: str, target: str) -> int:
        return sum(
            w.nbytes
            for w in self.writes
            if (w.source_participant_id, w.target_participant_id) == (writer, target)
        )


def _bindings(
    values: object, expected: set[str], label: str
) -> tuple[KVCacheResolvedRuntimeBinding, ...]:
    items = require_manifest_items(
        values, f"{label} bindings", KVCacheResolvedRuntimeBinding
    )
    ids = [b.participant_id for b in items]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise ValueError(f"{label} binding participants differ from selected operation")
    return tuple(sorted(items, key=lambda b: b.participant_id))


def _lower(
    plans: tuple[KVCacheLogicalTransferPlan, ...],
    source_bindings: tuple[KVCacheResolvedRuntimeBinding, ...],
    target_bindings: tuple[KVCacheResolvedRuntimeBinding, ...],
    limits: KVCacheTransferLimits,
) -> tuple[KVCacheWrite, ...]:
    source_ranges = {
        b.participant_id: index_runtime_ranges(b.ranges) for b in source_bindings
    }
    target_ranges = {
        b.participant_id: index_runtime_ranges(b.ranges) for b in target_bindings
    }
    source_regions = {
        b.participant_id: {r.region_id: r for r in b.regions} for b in source_bindings
    }
    target_regions = {
        b.participant_id: {r.region_id: r for r in b.regions} for b in target_bindings
    }
    writes: list[KVCacheWrite] = []
    total_bytes = 0
    work = 0
    last_regions: tuple[str, str] | None = None

    def append(
        writer: str,
        target: str,
        endpoint: str,
        source: int,
        destination: int,
        length: int,
        regions: tuple[str, str],
    ) -> None:
        nonlocal total_bytes, last_regions
        total_bytes += length
        if total_bytes > limits.max_bytes:
            raise ValueError("runtime transfer byte limit exceeded")
        while length:
            size = min(length, limits.max_batch_bytes)
            if (
                writes
                and last_regions == regions
                and (
                    writes[-1].source_participant_id,
                    writes[-1].target_participant_id,
                    writes[-1].endpoint,
                )
                == (writer, target, endpoint)
            ):
                previous = writes[-1]
                if (
                    previous.source_address + previous.nbytes == source
                    and previous.target_address + previous.nbytes == destination
                    and previous.nbytes + size <= limits.max_batch_bytes
                ):
                    writes[-1] = replace(previous, nbytes=previous.nbytes + size)
                else:
                    writes.append(
                        KVCacheWrite(
                            writer, target, endpoint, source, destination, size
                        )
                    )
            else:
                writes.append(
                    KVCacheWrite(writer, target, endpoint, source, destination, size)
                )
            if len(writes) > limits.max_operations:
                raise ValueError("runtime transfer operation limit exceeded")
            last_regions = regions
            source += size
            destination += size
            length -= size

    for plan in plans:
        for edge in plan.edges:
            sr = source_regions[edge.source_participant_id]
            tr = target_regions[edge.target_participant_id]
            key = (edge.global_layer_id, edge.component)
            ss = source_ranges[edge.source_participant_id].get(key, [])
            ts = target_ranges[edge.target_participant_id].get(key, [])
            work += len(ss) * len(ts)
            if work > limits.max_validation_work:
                raise ValueError("runtime lowering work limit exceeded")
            for source in ss:
                for target in ts:
                    copy = plan_range_copy(
                        source,
                        target,
                        edge.head_dim * edge.itemsize,
                        head_window=(
                            edge.global_head_start,
                            edge.global_head_start + edge.head_count,
                        ),
                    )
                    if copy is None:
                        continue
                    work += copy.segment_count
                    if work > limits.max_validation_work:
                        raise ValueError("runtime lowering expansion limit exceeded")
                    for source_offset, target_offset, nbytes in copy.segments():
                        append(
                            edge.source_participant_id,
                            edge.target_participant_id,
                            tr[target.region_id].endpoint,
                            sr[source.region_id].address + source_offset,
                            tr[target.region_id].address + target_offset,
                            nbytes,
                            (source.region_id, target.region_id),
                        )
    return tuple(writes)
