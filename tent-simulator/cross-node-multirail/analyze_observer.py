#!/usr/bin/env python3
"""Offline server-side analysis of tent-observer-v1; stdlib, no experiment driver.

Reviewed contracts: observer.cpp write_json/tv/bandwidth_release and
instrument_tent.py post/CQ/queue hooks. TV and reversal counts come from native
per-thread/context observations, never differences of averaged weights.
Allocation/post/CQ are distinct lifecycle bytes, not unique request goodput.
NIC names use only manifest.environment.nic_id_to_name, exported from the real
local topology. Per-NIC u_i uses successful Slice CQ bytes/s divided by the
same-Q, same-size S0/S1 capacity. It is relative usage, not physical utilization.
Active NICs come from environment.nic, including active NICs with zero traffic.
Queue statistics are sample-weighted software snapshots, not time-weighted
hardware queues. Effective sampling metadata comes only from the observer's
queue_sampling field, never the requested environment value.
SD is population SD; CV is null for zero mean. Variability
uses full windows only; empty-share windows break adjacent TV comparisons.
Epoch-wide statistics may span load changes and do not prove steady state.
All N/A values carry a reason. No files are written on import.
"""

import argparse
import json
import math
from pathlib import Path
import statistics

BASE_NS = 50_000_000
KINDS = ("allocation", "post", "cq_success")
QUEUES = {"worker_queue": ("inflight_slices", "inflight_slice_set_size", "requeue_overflow_size"),
          "cq_queue": ("getQuota", "maxCqe", "unused_zero")}
DIAGNOSTICS = ("cq_error", "cq_late_success", "cq_late_error", "post_rejected",
               "retry_endpoint", "retry_post", "retry_cq")


def na(reason):
    return {"value": None, "reason": reason}


def spread(values, minimum=1):
    values = sorted(v for v in values if v is not None)
    n = len(values)
    if n < minimum:
        return {**na(f"requires at least {minimum} eligible windows"), "samples": n}
    mean, sd = statistics.mean(values), statistics.pstdev(values)
    return {"samples": n, "mean": mean, "sd": sd, "cv": sd / mean if mean else None,
            "p95_minus_p5": values[(n * 95 + 99) // 100 - 1] - values[(n * 5 + 99) // 100 - 1]}


def losses(value, path="", inside_drops=False):
    found = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}/{key}"
            if ("overflow" in key or key in ("incomplete", "rejected_threads_lifetime") or inside_drops):
                if isinstance(item, (int, float)) and item != 0:
                    found[child] = item
            found.update(losses(item, child, inside_drops or key == "drops"))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found.update(losses(item, f"{path}/{i}", inside_drops))
    return found


def tv_counts(rows, kind, seconds, deltas):
    selected = [r for r in rows if r["kind"] == kind]
    n = sum(r["n"] for r in selected)
    if not n:
        return na("no observed comparable decisions in this thread/context")
    total = sum(r["values"][0]["sum"] for r in selected)
    result = {"comparisons": n, "tv_sum": total, "tv_per_second": total / seconds}
    if kind == "weight_tv":
        result["thresholds"] = [{"delta": delta,
            "events": sum(r["flags"][i] for r in selected),
            "events_per_second": sum(r["flags"][i] for r in selected) / seconds,
            "events_per_10000_comparisons": sum(r["flags"][i] for r in selected) * 10000 / n,
            "reversals": sum(r["flags"][i + 3] for r in selected),
            "reversals_per_second": sum(r["flags"][i + 3] for r in selected) / seconds}
            for i, delta in enumerate(deltas)]
    return result


def bucket_series(rows, start, end, width):
    windows = [{"start_ns": left, "end_ns": min(left + width, end),
                "bytes": {kind: {} for kind in KINDS}} for left in range(start, end, width)]
    for row in rows:
        if row["kind"] in KINDS:
            target = windows[row["bin"] * BASE_NS // width]["bytes"][row["kind"]]
            dev = str(row["dev"])
            target[dev] = target.get(dev, 0) + row["bytes"]
    metrics = {}
    for kind in KINDS:
        devices = sorted({dev for w in windows for dev in w["bytes"][kind]})
        previous, tv, comparisons = None, 0.0, 0
        for window in windows:
            total = sum(window["bytes"][kind].values())
            share = {dev: window["bytes"][kind].get(dev, 0) / total for dev in devices} if total else None
            window.setdefault("shares", {})[kind] = share
            window.setdefault("bytes_per_second", {})[kind] = total * 1e9 / (window["end_ns"] - window["start_ns"])
            if window["end_ns"] - window["start_ns"] != width:
                continue
            if previous is not None and share is not None:
                tv += sum(abs(share[d] - previous[d]) for d in devices) / 2
                comparisons += 1
            previous = share
        full = [w for w in windows if w["end_ns"] - w["start_ns"] == width]
        metrics[kind] = {"rate_bytes_per_second": spread([w["bytes_per_second"][kind] for w in full]),
            "share_by_dev": {dev: spread([w["shares"][kind][dev] for w in full if w["shares"][kind] is not None]) for dev in devices},
            "adjacent_share_tv": {"comparisons": comparisons, "sum": tv,
                                  "per_second": tv * 1e9 / (end - start)} if comparisons else na("no adjacent nonempty full windows")}
    return {"window_ns": width, "windows": windows, "variability": metrics}


def aligned(series, start, end, width):
    return [(w["start_ns"], w["end_ns"]) for w in series] == [
        (left, min(left + width, end)) for left in range(start, end, width)]


def nic_reference(manifest, capacity):
    """Resolve enabled NICs through the verified map; never infer them from traffic."""
    environment, config = manifest.get("environment", {}), manifest.get("stream_config", {})
    mapping, whitelist = environment.get("nic_id_to_name"), environment.get("nic")
    if not isinstance(mapping, dict) or not isinstance(whitelist, str):
        return na("verified nic_id_to_name and active environment.nic are required")
    if any(not isinstance(name, str) or not str(dev).isdigit() for dev, name in mapping.items()):
        return na("invalid verified NicID-to-name mapping")
    mapping = {str(dev): name for dev, name in mapping.items()}
    active = [name.strip() for name in whitelist.split(",")]
    if len(active) not in (1, 2) or len(set(active)) != len(active):
        return na("requires one or two explicitly enabled distinct NICs")
    modes = {"erdma_0": "S0", "erdma_1": "S1"}
    if any(name not in modes or list(mapping.values()).count(name) != 1 for name in active):
        return na("active NICs must uniquely map to the known S0/S1 modes")
    entry = (capacity or {}).get("sizes", {}).get(str(config.get("size")), {})
    if (capacity or {}).get("schema_version") != 1 or (capacity or {}).get("units") != "bytes_per_second":
        return na("supported measured capacity.json with bytes_per_second units is required")
    if type(entry.get("q")) is not int or entry["q"] != config.get("window"):
        return na("capacity Q differs from the run, or same-size capacity is missing")
    context = capacity.get("context", {})
    for key, field in (("tent_library_sha256", "tent_sha256"), ("stream_library_sha256", "stream_sha256")):
        if context.get(key) is None or context[key] != environment.get(field):
            return na("matching capacity/run library fingerprints are required")
    references = {}
    for name in active:
        value = entry.get("mode_capacity_bytes_per_second", {}).get(modes[name])
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            return na(f"positive same-Q {modes[name]} capacity is missing")
        references[name] = value
    return {"nic_id_to_name": mapping, "active_nics": active, "q": entry["q"],
            "size": config["size"], "capacity_bytes_per_second": references,
            "formulas": {"u_i": "cq_success_bytes_per_second_i / C_i",
                         "jain": "(u_0 + u_1)^2 / (2 * (u_0^2 + u_1^2))",
                         "imbalance": "I = abs(u_0 - u_1) / (u_0 + u_1)"},
            "scope": "successful Slice CQ rate / fixed-single-NIC application capacity; full runtime configuration not available"}


def nic_utilization(window, reference):
    """Return CQ-based u_i and two-active-NIC Jain/I for this actual window duration."""
    if "active_nics" not in reference:
        return dict(reference)
    mapping, active = reference["nic_id_to_name"], reference["active_nics"]
    payload = dict.fromkeys(active, 0)
    for dev, count in window["bytes"]["cq_success"].items():
        if dev not in mapping or mapping[dev] not in payload:
            return na("CQ NicID is absent from the verified map or outside the active NIC set")
        payload[mapping[dev]] += count
    duration = (window["end_ns"] - window["start_ns"]) / 1e9
    rate = {name: count / duration for name, count in payload.items()}
    usage = {name: value / reference["capacity_bytes_per_second"][name] for name, value in rate.items()}
    total, squares = sum(usage.values()), sum(value * value for value in usage.values())
    result = {"cq_success_bytes_per_second": rate, "u_i": usage}
    if len(active) != 2:
        result.update(jain=na("only one active NIC; dual-rail Jain is not applicable"),
                      imbalance=na("only one active NIC; dual-rail I is not applicable"))
    elif not total:
        result.update(jain=na("both active NICs have zero successful CQ bytes"),
                      imbalance=na("both active NICs have zero successful CQ bytes"))
    else:
        result.update(jain=total * total / (2 * squares),
                      imbalance=abs(usage[active[0]] - usage[active[1]]) / total)
    return result


def analyze(observer, manifest, capacity=None, request_summary=None, request_windows=None,
            threads=None, contexts=None):
    """Return SUMMARY and aligned windows; filters never merge context identities."""
    if observer["schema"] != "tent-observer-v1" or observer["clock"] != "CLOCK_MONOTONIC":
        raise ValueError("unsupported observer schema/clock")
    if observer["window_ns"] != BASE_NS or observer["deltas"] != [0.001, 0.01, 0.05]:
        raise ValueError("unsupported native bin or flag thresholds")
    start, end = manifest["measurement_start_ns"], manifest["measurement_end_ns"]
    if type(start) is not int or type(end) is not int or end <= start:
        raise ValueError("invalid manifest measurement interval")
    loss = {**losses(observer, "observer"), **losses(manifest, "manifest")}
    result = {"schema": "tent-observer-analysis-v1", "incomplete": bool(loss), "loss_evidence": loss,
        "SUMMARY": {"measurement_start_ns": start, "measurement_end_ns": end,
            "counts_scope": "observed only; completeness is not asserted when incomplete=true",
            "filters": {"threads": threads, "contexts": contexts},
            "queue_sampling": observer["queue_sampling"] if observer.get("queue_sampling") is not None
                else na("observer did not export actual queue_sampling; requested stride does not prove effective sampling"),
            "native_accounting": {k: manifest.get(k) for k in ("accepted", "success", "failure", "pending", "native_return_code")},
            "unavailable": {**observer.get("unavailable", {}),
                "queue_p99": "N/A: aggregate sampled min/max/sum cannot recover quantiles",
                "phase_specific_reversal": "N/A: native counters retain direction across load-phase boundaries",
                "physical_link_utilization": "N/A: reference capacities are measured application goodput, not physical link capacities"}}, "windows": {}}
    summary = result["SUMMARY"]
    if (observer["start_ns"], observer["end_ns"]) != (start, end):
        summary["observer_metrics"] = na("observer epoch differs from measurement; aggregated rows cannot be split or reset TV history")
        return result
    seconds, rows, groups = (end - start) / 1e9, [], {}
    for thread in observer["threads"]:
        slot = thread["thread_slot"]
        if threads is not None and slot not in threads:
            continue
        for context in thread["contexts"]:
            key = f"{slot}:{context['id']}"
            if contexts is None or key in contexts:
                if key in groups:
                    raise ValueError("duplicate thread/context identity")
                groups[key] = {"context": context, "rows": []}
        seen = set()
        for row in thread["rows"]:
            key = f"{slot}:{row['context']}"
            if key not in groups:
                if contexts is None:
                    raise ValueError("row refers to unknown context")
                continue
            identity = (row["bin"], row["context"], row["dev"], row["kind"])
            if identity in seen:
                raise ValueError("duplicate native row")
            seen.add(identity)
            if type(row["bin"]) is not int or not 0 <= row["bin"] * BASE_NS < end - start:
                raise ValueError("row bin outside epoch")
            if type(row["n"]) is not int or type(row["bytes"]) is not int or row["n"] <= 0 or row["bytes"] < 0 or len(row["flags"]) != 6 or len(row["values"]) != 3:
                raise ValueError("invalid row counters/shape")
            if any(type(f) is not int or f < 0 for f in row["flags"]):
                raise ValueError("invalid flags")
            if any(type(v[k]) not in (int, float) or not math.isfinite(v[k]) for v in row["values"] for k in ("sum", "mean", "min", "max", "last")):
                raise ValueError("nonfinite/missing row statistics")
            copied = {**row, "thread_context": key}
            groups[key]["rows"].append(copied)
            rows.append(copied)
    if not rows:
        summary["observer_metrics"] = na("no native rows after filtering; absence is not proof of zero activity")
        return result
    summary.update(consumed_weights=[], selection_diagnostics=[], clamp_updates=[], queue_samples=[])
    for key, group in groups.items():
        ctx, selected = group["context"], group["rows"]
        if ctx["decisions"]:
            entry = {"thread_context": key, "definition": ctx,
                "weight": tv_counts(selected, "weight_tv", seconds, observer["deltas"]),
                "allocation": tv_counts(selected, "allocation_tv", seconds, observer["deltas"])}
            target = "consumed_weights" if ctx["weight_semantics"] == "consumed_inverse_score" else "selection_diagnostics"
            entry["interpretation"] = "consumed batch weight" if target == "consumed_weights" else {0: "single-slice diagnostic", 2: "probe diagnostic", 3: "non-smart selection"}.get(ctx["mode"], "unclassified; not a consumed weight")
            summary[target].append(entry)
        for row in selected:
            bounds = {"start_ns": start + row["bin"] * BASE_NS, "end_ns": min(start + (row["bin"] + 1) * BASE_NS, end)}
            if row["kind"] == "release_bw":
                flags = row["flags"]
                summary["clamp_updates"].append({**bounds, "thread_context": key, "dev": row["dev"], "updates": row["n"],
                    "new_equals_min": flags[0], "new_equals_max": flags[1],
                    "unclamped_below_min": flags[2], "unclamped_above_max": flags[3],
                    "strict_clamp_fraction": (flags[2] + flags[3]) / row["n"]})
            if row["kind"] in QUEUES:
                summary["queue_samples"].append({**bounds, "thread_context": key, "dev": row["dev"],
                    "kind": row["kind"], "samples": row["n"],
                    "sample_statistics": dict(zip(QUEUES[row["kind"]], row["values"]))})
    clamp = summary["clamp_updates"]
    summary["clamp_totals"] = {k: sum(r[k] for r in clamp) for k in (
        "updates", "new_equals_min", "new_equals_max", "unclamped_below_min", "unclamped_above_max")} if clamp else na("no observed bandwidth-learning updates")
    if not summary["consumed_weights"]:
        summary["consumed_weights"] = na("no consumed_inverse_score context with observed decisions")
    summary["io_diagnostics"] = {kind: {"events": sum(r["n"] for r in rows if r["kind"] == kind),
        "bytes": sum(r["bytes"] for r in rows if r["kind"] == kind)} for kind in DIAGNOSTICS}
    summary["reversal_semantics"] = "flags[3:6]: negative dot product with previous above-threshold delta in the same thread/context; flags[0:3]: TV > delta"
    reference = nic_reference(manifest, capacity)
    if threads is not None or contexts is not None:
        reference = na("thread/context filtering may omit CQ traffic; whole-NIC utilization cannot be inferred")
    summary["nic_capacity_reference"] = reference
    for width in (BASE_NS, 250_000_000, 1_000_000_000):
        label = f"{width // 1_000_000}ms"
        result["windows"][label] = bucket_series(rows, start, end, width)
        request = (request_windows or {}).get("throughput", {}).get(label)
        grid = result["windows"][label]
        for window in grid["windows"]:
            window["nic_utilization"] = nic_utilization(window, reference)
            window["nic_utilization"]["incomplete"] = result["incomplete"]
        if request is None or not aligned(request, start, end, width):
            grid["request_throughput"] = na("request completion windows missing or not exactly aligned")
        else:
            for window, application in zip(grid["windows"], request):
                window["request_completion"] = application
            grid["request_throughput"] = spread([w["bits_per_second"] for w in request if w["end_ns"] - w["start_ns"] == width])
    latency = (request_windows or {}).get("latency", {})
    lw, items = latency.get("window_ns"), latency.get("windows", [])
    if type(lw) is int and lw > 0 and aligned(items, start, end, lw):
        values = [w["p99_end_to_end_ns"] for w in items if w["success_requests"] >= 500 and w["end_ns"] - w["start_ns"] == lw and w["p99_end_to_end_ns"] is not None]
        baseline_16m = (str(manifest.get("label", "")).startswith("baseline-")
                        and manifest.get("stream_config", {}).get("size") == 16 * 1024 * 1024)
        summary["request_p99"] = {"window_ns": lw, "eligible_windows": len(values), "total_windows": len(items),
            "coverage": len(values) / len(items),
            "variability_ns": na("frozen 16MiB baseline policy: window-P99 variability is not evaluated; retain global quantiles") if baseline_16m else spread(values, 20),
            "arrival_windows": items,
            "semantics": "success-conditional, planned-arrival cohorts; no resampling of P99 to 50ms"}
    else:
        summary["request_p99"] = na("aligned arrival-cohort windows not supplied")
    if request_summary and (request_summary.get("measurement_start_ns"), request_summary.get("measurement_end_ns")) == (start, end):
        summary["request_summary"] = request_summary
    else:
        summary["request_summary"] = na("request summary missing or measurement interval differs")
    entry = (capacity or {}).get("sizes", {}).get(str(manifest.get("stream_config", {}).get("size")), {})
    reference = entry.get("reference_bytes_per_second")
    cap_context, environment = (capacity or {}).get("context", {}), manifest.get("environment", {})
    matching_libraries = all(cap_context.get(name) is not None and cap_context[name] == environment.get(field)
        for name, field in (("tent_library_sha256", "tent_sha256"), ("stream_library_sha256", "stream_sha256")))
    compatible = (capacity or {}).get("schema_version") == 1 and (capacity or {}).get("units") == "bytes_per_second" and matching_libraries
    summary["capacity_utilization"] = na("matching measured D0 capacity, Q, library fingerprints and request summary required")
    if compatible and type(reference) in (int, float) and math.isfinite(reference) and reference > 0 and reference == entry.get("mode_capacity_bytes_per_second", {}).get("D0") and entry.get("q") == manifest.get("stream_config", {}).get("window") and "throughput" in summary["request_summary"]:
        summary["capacity_utilization"] = {"reference_bytes_per_second": reference,
            "value": summary["request_summary"]["throughput"]["bits_per_second"] / 8 / reference,
            "saturation_confirmed": entry.get("saturation_confirmed"), "scope": "application goodput / supplied D0 reference; size/Q/library fingerprints checked, full runtime configuration not available"}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("observer", "manifest", "capacity", "request-summary", "request-windows"):
        parser.add_argument("--" + name, type=Path, required=name in ("observer", "manifest"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--thread", type=int, action="append")
    parser.add_argument("--context", action="append", help="repeatable thread_slot:context_id")
    args = parser.parse_args()
    try:
        def read(path):
            return json.loads(path.read_text(encoding="utf-8")) if path else None
        report = analyze(read(args.observer), read(args.manifest), read(args.capacity),
                         read(args.request_summary), read(args.request_windows), args.thread, args.context)
        content = json.dumps(report, indent=2, allow_nan=False) + "\n"
        output, repo = args.output.expanduser().resolve(), Path(__file__).resolve().parents[2]
        if output == repo or repo in output.parents:
            raise ValueError("analysis output must be outside repository")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(content)
        print("SUMMARY", output, "INCOMPLETE", report["incomplete"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
