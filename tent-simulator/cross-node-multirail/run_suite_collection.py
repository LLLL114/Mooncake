#!/usr/bin/env python3
# Collection policy copy; frozen source SHA256: ffd01df8742f5736444e41592531574d6d17f45c6a6355fec1cba5e2eba27e91
"""Serial, resumable native TENT suites (stdlib only).

Run this with the current mooncake environment's Python: children use exactly
sys.executable and the adjacent native_sender.py. No SSH, shell, build, or
receiver management is performed. --dry-run prints JSON and performs no writes
or subprocess calls. All logs, state, native artifacts and capacity.json belong
under the explicit outside-repository output root.

pilot: one saturated Q=1 run for each mode/size (9 points), using CLI timing.
calibrate: 3 modes x 3 sizes x Q=1/8/32 x 3 repeats, always 10s + 1s warmup.
baseline: 3 modes x 3 sizes x 20/60/90%/saturated x --repetitions; default 180.
Baseline timing is frozen by size: 64KiB=1s P99/at least 60s measurement,
1MiB=5s/at least 120s, 16MiB=1s/at least 60s. --seconds is a lower bound.
16MiB window-P99 variability remains N/A; retain global latency quantiles.
Only --uniform-latency-window explicitly applies --latency-window-ms to every
baseline size. It does not reduce the size-specific minimum measurement time.
dynamic/local: declarative proposals only; deliberately never launch a sender.

Calibration uses medians across all three repeats, then selects the smallest Q
reaching 95% of the best D0 median. Each mode's capacity is its median at that
same Q. Q32/Q8 > 1.05 means saturation is not confirmed; the frozen reference
is still a measured reference, not a claim of peak capacity. No failed sample
is dropped or replaced silently. A frozen capacity file is never overwritten.

State is atomically saved before and after every attempt. Successful cases are
skipped only for an identical full-parameter SHA256 and readable valid outputs.
Failure stops the suite, except a validated expected baseline overload, which
is retained as completed_overload or completed_overload_unverified (never normal successful latency).
--retry-failed explicitly permits another attempt,
retaining all earlier logs/state; after interruption, first ensure the previous
native process has exited. One local advisory lock prevents concurrent suites
under the same root. Repetitions added later reuse earlier successful cases.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SENDER = HERE / "native_sender_extended.py"
BASE = Path("/root/mooncake-tent-multirdma-output/cross-node-multirail")
MODES = {"S0": "erdma_0", "S1": "erdma_1", "D0": "erdma_0,erdma_1"}
SIZES = (64 * 1024, 1024 * 1024, 16 * 1024 * 1024)
QS = (32, 64, 128)
BASELINE_TIMING = {64 * 1024: (60.0, 1000), 1024 * 1024: (120.0, 5000),
                   16 * 1024 * 1024: (60.0, 1000)}
SHUFFLE_SEED = 20260910


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def case_hash(case):
    """Stable SHA256 of the entire case identity, without suite ordering."""
    return hashlib.sha256(_json(case).encode()).hexdigest()


def _read(path):
    with Path(path).open(encoding="utf-8") as stream:
        result = json.load(stream)
    if not isinstance(result, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return result


def _save(path, value, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".suite-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)  # Publish completely, without replacing a frozen file.
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _outside_repo(path):
    path = Path(path).expanduser().resolve()
    if path == REPO or REPO in path.parents:
        raise ValueError(f"output must be outside repository: {path}")
    return path


def _context(args):
    context = {
        "python": sys.executable, "sender": str(SENDER),
        "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "numa_node_requested": os.environ.get("TENT_NUMA_NODE"),
        "sender_sha256": hashlib.sha256(SENDER.read_bytes()).hexdigest(),
        "peer": args.peer, "tent_library": str(Path(args.tent_library).expanduser().resolve()),
        "stream_library": str(Path(args.stream_library).expanduser().resolve()),
        "latency_window_ms": args.latency_window_ms,
        "observer_queue_poll_stride_requested": os.environ.get("TENT_OBS_QUEUE_POLL_STRIDE", "1"),
    }
    # Dry-run works on a local machine without the server's shared libraries.
    for name in ("tent_library", "stream_library"):
        path = Path(context[name])
        digest = hashlib.sha256()
        if path.is_file():
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            context[name + "_sha256"] = digest.hexdigest()
        else:
            context[name + "_sha256"] = None
    return context


def _case(args, context, phase, mode, size, q, repeat, load, rate=0.0, reference=None):
    label = f"{phase}-{mode}-{size}-Q{q}-{load}-r{repeat}"
    seconds = float(args.seconds)
    if phase == "baseline":
        minimum_seconds, latency_window_ms = BASELINE_TIMING[size]
        seconds = max(seconds, minimum_seconds)
        uniform = getattr(args, "uniform_latency_window", False)
        if uniform:
            latency_window_ms = args.latency_window_ms
        context = {**context, "latency_window_ms": latency_window_ms}
    parameters = {
        "nic": MODES[mode], "peer": context["peer"], "size": size, "window": q,
        "callers": 1, "seconds": 10.0 if phase == "calibrate" else seconds,
        "warmup": 1.0 if phase == "calibrate" else float(args.warmup),
        "rate": float(rate), "steps": None, "label": label, "rr": False,
        "observer_off": False, "alpha": None, "workers": None, "jitter": None,
        "default_gbps": None, "config_override": None,
        "latency_window_ms": context["latency_window_ms"],
        "tent_library": context["tent_library"], "stream_library": context["stream_library"],
        "output_root": str((args.output_root / "runs").resolve()),
    }
    case = {"schema_version": 1, "phase": phase, "mode": mode, "size": size,
            "q": q, "repeat": repeat, "load": load, "parameters": parameters,
            "context": context, "capacity_reference": reference}
    if phase == "baseline":
        case["baseline_timing"] = {
            "policy": "explicit_uniform_override" if uniform else "frozen_by_size",
            "minimum_measurement_seconds": minimum_seconds,
            "measurement_seconds": seconds, "latency_window_ms": latency_window_ms,
            "minimum_successes_per_p99_window": 500, "minimum_valid_p99_windows": 20,
            "window_p99_variability": "N/A: 16MiB; retain global quantiles" if size == 16 * 1024 * 1024
                else "report only when both sample and valid-window thresholds are met",
        }
    return case


def native_command(case):
    """Build an argv list using only documented native_sender.py options."""
    deadline = math.ceil(case["parameters"]["seconds"] + case["parameters"]["warmup"] + 200)
    command = ["timeout", "--signal=TERM", "--kill-after=10s", str(deadline),
               case["context"]["python"], "-u", case["context"]["sender"]]
    for key, value in case["parameters"].items():
        if value is None or value is False:
            continue
        command.append("--" + key.replace("_", "-"))
        if value is not True:
            command.append(str(value))
    return command


def dynamic_cases(args, capacity):
    """1MiB D0 step-load proposals; no assumption about native --steps wiring."""
    entry = capacity["sizes"][str(1024 * 1024)]
    return [{"phase": "dynamic", "mode": "D0", "size": 1024 * 1024,
             "q": entry["q"], "repeat": repeat, "executable": False,
             "reason": "proposal only; stream step execution must be validated by the native driver owner",
             "warmup": args.warmup, "total_seconds": args.seconds,
             "segments": [{"fraction_of_duration": 1 / 3, "load_fraction": fraction,
                           "rate_bytes_per_second": fraction * entry["reference_bytes_per_second"]}
                          for fraction in (0.2, 0.8, 0.2)]}
            for repeat in range(1, args.repetitions + 1)]


def local_cases(args, capacity):
    """1MiB D0 caller/alignment proposals at fixed aggregate rate and total Q."""
    entry = capacity["sizes"][str(1024 * 1024)]
    return [{"phase": "local", "mode": "D0", "size": 1024 * 1024,
             "total_q": entry["q"], "repeat": repeat, "callers": callers,
             "arrival_alignment": alignment, "load_fraction": fraction,
             "aggregate_rate_bytes_per_second": fraction * entry["reference_bytes_per_second"],
             "seconds": args.seconds, "warmup": args.warmup, "executable": False,
             "reason": "native stream currently supports callers=1 only; alignment API is unverified"}
            for repeat in range(1, args.repetitions + 1) for fraction in (0.6, 0.9)
            for callers in (1, 4, 8) for alignment in ("synchronized", "staggered")]


def generate_cases(args, capacity=None):
    """Return deterministic cases; calibration is always exactly 81 points."""
    if args.phase in ("dynamic", "local"):
        return (dynamic_cases if args.phase == "dynamic" else local_cases)(args, capacity)
    context = _context(args)
    result = []
    repeats = 3 if args.phase == "calibrate" else 1 if args.phase == "pilot" else args.repetitions
    for repeat in range(1, repeats + 1):
        block = []
        for mode in MODES:
            for size in SIZES:
                if args.phase == "baseline":
                    entry = capacity["sizes"][str(size)]
                    reference = {"capacity_sha256": case_hash(capacity),
                                 "d0_bytes_per_second": entry["reference_bytes_per_second"],
                                 "saturation_confirmed": entry["saturation_confirmed"]}
                    for load, fraction in (("20pct", 0.2), ("60pct", 0.6), ("90pct", 0.9), ("saturated", 0)):
                        case = _case(args, context, args.phase, mode, size, entry["q"], repeat,
                                     load, fraction * entry["reference_bytes_per_second"], reference)
                        case["mode_capacity"] = entry["mode_capacity_bytes_per_second"][mode]
                        case["offered_exceeds_mode_capacity"] = case["parameters"]["rate"] > case["mode_capacity"]
                        case["latency_interpretation"] = (
                            "mode_capacity is bytes/s at the shared Q; offered load uses the shared D0 reference. "
                            "When this mode cannot sustain that input, queueing/P99 is an overload comparison, "
                            "not evidence of an effect caused by weight changes, even if the process exits normally.")
                        block.append(case)
                else:
                    for q in QS if args.phase == "calibrate" else (1,):
                        block.append(_case(args, context, args.phase, mode, size, q, repeat, "saturated"))
        random.Random(SHUFFLE_SEED + repeat).shuffle(block)
        result.extend(block)
    return result


def read_goodput(run_path, case=None):
    """Read verified completion goodput from summary bytes / manifest duration.

    Do not use manifest.success * size: that count includes drain completions.
    Failed, pending, incomplete, inconsistent or shortened exports are errors.
    """
    root = Path(run_path)
    manifest, summary = _read(root / "manifest.json"), _read(root / "summary.json")
    if manifest.get("native_return_code") != 0 or manifest.get("data_verified") is not True:
        raise ValueError(f"{root}: native return code or data verification failed")
    if manifest.get("drained") is not True or manifest.get("requests_output_complete") is not True:
        raise ValueError(f"{root}: missing drain or complete-output evidence")
    if manifest.get("overflow") or manifest.get("input_stopped_early") or manifest.get("watchdog_required"):
        raise ValueError(f"{root}: incomplete native run")
    for counts in (manifest, manifest.get("total", {})):
        if counts.get("failure") != 0 or counts.get("pending") != 0:
            raise ValueError(f"{root}: failed/pending requests or missing accounting")
        if type(counts.get("accepted")) is not int or counts["accepted"] != counts.get("success"):
            raise ValueError(f"{root}: accepted/success conservation mismatch")
    start, end = manifest.get("measurement_start_ns"), manifest.get("measurement_end_ns")
    if type(start) is not int or type(end) is not int or end <= start:
        raise ValueError(f"{root}: invalid measurement interval")
    if (summary.get("measurement_start_ns"), summary.get("measurement_end_ns")) != (start, end):
        raise ValueError(f"{root}: summary and manifest measurement intervals differ")
    payload = summary.get("throughput", {}).get("success_bytes")
    if type(payload) is not int or payload < 0:
        raise ValueError(f"{root}: missing completion payload bytes")
    goodput = payload * 1_000_000_000 / (end - start)
    reported = summary["throughput"].get("bits_per_second")
    if not isinstance(reported, (int, float)) or not math.isclose(reported, goodput * 8, rel_tol=1e-9):
        raise ValueError(f"{root}: summary goodput disagrees with bytes / measurement duration")
    if case is not None:
        config, parameters = manifest.get("stream_config", {}), case["parameters"]
        for key in ("size", "window", "callers", "seconds", "warmup"):
            if config.get(key) != parameters[key]:
                raise ValueError(f"{root}: stream config differs from case: {key}")
        if config.get("rate_bytes_per_second") != parameters["rate"]:
            raise ValueError(f"{root}: stream rate differs from case")
        if manifest.get("environment", {}).get("nic") != parameters["nic"]:
            raise ValueError(f"{root}: NIC mode differs from case")
        for name, field in (("tent_library", "tent_sha256"), ("stream_library", "stream_sha256")):
            expected = case["context"][name + "_sha256"]
            if expected is not None and manifest["environment"].get(field) != expected:
                raise ValueError(f"{root}: native library fingerprint differs from case: {name}")
        if end - start != round(parameters["seconds"] * 1_000_000_000):
            raise ValueError(f"{root}: measurement did not reach planned duration")
    return goodput


def read_completed_overload(run_path, case, exit_code, allow_unexpected=False):
    """Validate complete raw artifacts for an expected, drained baseline rc=1.

    Unsubmitted accepted requests remain in requests.jsonl and the excluded
    export. Never rewrite native failures, accounting, stop flags or timings.
    """
    if not (allow_unexpected and _unexpected_overload_case(case, exit_code)):
        if (case["phase"] != "baseline" or case.get("offered_exceeds_mode_capacity") is not True
                or not case["parameters"]["rate"] > case.get("mode_capacity", float("inf"))):
            raise ValueError("case is not an expected fixed-input baseline overload")
    root = Path(run_path)
    if exit_code not in (0, 1):
        raise ValueError("unexpected sender exit code for a controlled overload")
    failure_path = root / "failure.json"
    if exit_code == 1 or failure_path.exists():
        failure = _read(failure_path)
        if (failure.get("type") != "RuntimeError" or failure.get("pending_unsafe") is not False
                or failure.get("error") != "native stream returned 1; see saved terminal accounting"):
            raise ValueError("sender failure is not the documented native rc=1 propagation")
    names = ("manifest", "summary", "stream-summary", "windows", "correctness",
             "arguments", "stream-config", "tent-config", "observer")
    artifacts = {name: _read(root / (name + ".json")) for name in names}
    manifest, summary, native = artifacts["manifest"], artifacts["summary"], artifacts["stream-summary"]
    if manifest.get("native_return_code") != 1 or native.get("return_code") != 1:
        raise ValueError("expected native controlled-stop return code 1")
    if (any(type(manifest.get(k)) is not bool for k in ("overflow", "input_stopped_early"))
            or not (manifest["overflow"] or manifest["input_stopped_early"])):
        raise ValueError("controlled overload requires explicit overflow/early-stop evidence")
    if (manifest.get("data_verified") is not True or artifacts["correctness"].get("passed") is not True
            or manifest.get("drained") is not True or manifest.get("requests_output_complete") is not True
            or manifest.get("watchdog_required") is not False or manifest.get("outstanding_batches") != 0):
        raise ValueError("overload verification, drain or output completeness failed")
    if any(manifest.get(k) != v for k, v in native.items()) or summary.get("complete_run_accounting") != native:
        raise ValueError("native/manifest/request-summary accounting differs")
    if artifacts["observer"].get("incomplete") is not False:
        raise ValueError("observer export is incomplete or lacks completeness evidence")
    parameters = case["parameters"]
    if any(artifacts["arguments"].get(k) != v for k, v in parameters.items()):
        raise ValueError("overload sender arguments differ from the hashed case")
    config = manifest.get("stream_config", {})
    if config != artifacts["stream-config"] or any(config.get(k) != parameters[k]
            for k in ("size", "window", "callers", "seconds", "warmup")):
        raise ValueError("overload stream configuration differs from the case")
    if config.get("rate_bytes_per_second") != parameters["rate"]:
        raise ValueError("overload offered rate differs from the case")
    environment = manifest.get("environment", {})
    if environment.get("nic") != parameters["nic"]:
        raise ValueError("overload NIC mode differs from the case")
    for name, field in (("tent_library", "tent_sha256"), ("stream_library", "stream_sha256")):
        expected = case["context"][name + "_sha256"]
        if expected is not None and environment.get(field) != expected:
            raise ValueError("overload native library fingerprint differs from the case")
    start, end = manifest.get("measurement_start_ns"), manifest.get("measurement_end_ns")
    if type(start) is not int or type(end) is not int or not 0 < end - start <= round(parameters["seconds"] * 1e9):
        raise ValueError("overload lacks a valid bounded measurement interval")
    if (summary.get("measurement_start_ns"), summary.get("measurement_end_ns")) != (start, end):
        raise ValueError("overload summary interval differs from manifest")
    with (root / "requests.jsonl").open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    if len({r["request_id"] for r in records}) != len(records):
        raise ValueError("duplicate overload request IDs")
    if any(r.get("phase") not in ("warmup", "measurement") or r.get("status") not in ("success", "failure") for r in records):
        raise ValueError("overload export contains unresolved or unclassified requests")
    for counts, selected in ((manifest.get("measurement", {}), [r for r in records if r["phase"] == "measurement"]),
                             (manifest.get("warmup", {}), [r for r in records if r["phase"] == "warmup"]),
                             (manifest.get("total", {}), records)):
        if any(type(counts.get(k)) is not int or counts[k] < 0 for k in ("accepted", "success", "failure", "pending")):
            raise ValueError("overload terminal accounting is missing or invalid")
        if counts["pending"] != 0 or counts["accepted"] != counts["success"] + counts["failure"] + counts["pending"]:
            raise ValueError("overload did not drain or failed acceptance conservation")
        if counts["accepted"] != len(selected) or any(counts[k] != sum(r["status"] == k for r in selected) for k in ("success", "failure")):
            raise ValueError("overload raw request records do not conserve native terminal counts")
    excluded = [r for r in records if r.get("submitted_ns") is None or r.get("finished_ns") is None]
    if summary.get("excluded_non_submitted_terminal_records") != len(excluded):
        raise ValueError("overload excluded-request count differs from raw records")
    excluded_path = root / "unsubmitted-or-unresolved.json"
    if excluded or excluded_path.exists():
        if json.loads(excluded_path.read_text(encoding="utf-8")) != excluded:
            raise ValueError("overload excluded requests were not preserved completely")
    payload = sum(r["bytes"] for r in records if r["status"] == "success"
                  and r.get("submitted_ns") is not None and r.get("finished_ns") is not None
                  and start <= r["finished_ns"] < end)
    goodput = payload * 1e9 / (end - start)
    throughput = summary.get("throughput", {})
    if throughput.get("success_bytes") != payload or not math.isclose(throughput.get("bits_per_second", -1), goodput * 8, rel_tol=1e-9):
        raise ValueError("overload goodput differs from successful measurement-time completions")
    for label, width in (("50ms", 50_000_000), ("250ms", 250_000_000), ("1000ms", 1_000_000_000)):
        windows = artifacts["windows"].get("throughput", {}).get(label, [])
        if ([(w["start_ns"], w["end_ns"]) for w in windows]
                != [(left, min(left + width, end)) for left in range(start, end, width)]
                or sum(w["success_bytes"] for w in windows) != payload):
            raise ValueError("overload completion windows are incomplete or inconsistent")
    latency = artifacts["windows"].get("latency", {})
    width = parameters["latency_window_ms"] * 1_000_000
    arrivals = [r for r in records if r.get("submitted_ns") is not None and r.get("finished_ns") is not None
                and start <= r["planned_ns"] < end]
    if (latency.get("window_ns") != width
            or [(w["start_ns"], w["end_ns"]) for w in latency.get("windows", [])]
            != [(left, min(left + width, end)) for left in range(start, end, width)]
            or sum(w["requests"] for w in latency.get("windows", [])) != len(arrivals)):
        raise ValueError("overload arrival windows are incomplete or omit terminal records")
    if any(not isinstance(summary.get(k), dict) for k in ("latency_success", "latency_failure_wait", "measurement", "observed", "conservation")):
        raise ValueError("overload request summary is incomplete")
    return {"normal_successful_latency": False, "goodput_bytes_per_second": goodput,
            "native_accounting": native, "excluded_non_submitted_terminal_records": len(excluded),
            "latency_interpretation": "controlled overloaded/shortened run; never pool with normal successful latency"}


def read_completed_overload_unverified(run_path, case, exit_code, allow_unexpected=False):
    """Accept only an empty final verification after a fully accounted controlled stop."""
    p = case["parameters"]
    if not (allow_unexpected and _unexpected_overload_case(case, exit_code)):
        if (case["phase"] != "baseline" or case.get("offered_exceeds_mode_capacity") is not True
                or not p["rate"] > case.get("mode_capacity", float("inf")) or exit_code != 1):
            raise ValueError("not an expected unverified baseline overload")
    root = Path(run_path)
    a = {name: _read(root / (name + ".json")) for name in
         ("manifest", "stream-summary", "correctness", "failure", "observer",
          "arguments", "stream-config", "tent-config", "topology")}
    m, n, config = a["manifest"], a["stream-summary"], a["stream-config"]
    if (m.get("native_return_code") != 1 or n.get("return_code") != 1
            or "data_verified" not in m or m["data_verified"] is not None
            or any(m.get(k) != v for k, v in n.items())):
        raise ValueError("unverified overload checkpoint/native evidence differs")
    if (a["failure"].get("type") != "RuntimeError" or a["failure"].get("pending_unsafe") is not False
            or a["failure"].get("error") != "receiver final full payload/guard mismatch"
            or a["correctness"].get("passed") is not False or a["correctness"].get("checks") != []
            or n.get("final_epochs") != [None] * p["window"]):
        raise ValueError("not the empty-checks/all-null-epochs receiver failure")
    if (any(type(n.get(k)) is not bool for k in ("overflow", "input_stopped_early"))
            or not (n["overflow"] or n["input_stopped_early"])
            or n.get("drained") is not True or n.get("requests_output_complete") is not True
            or n.get("watchdog_required") is not False or n.get("outstanding_batches") != 0
            or n.get("start_missed") is not False or n.get("failure_reason") is not None
            or n.get("outstanding_slots") != [] or a["observer"].get("incomplete") is not False):
        raise ValueError("unverified overload lacks controlled-stop/drain/complete-output evidence")
    if (any(a["arguments"].get(k) != v for k, v in p.items())
            or a["arguments"].get("gate_batch") is not False
            or a["arguments"].get("arrival_alignment") != "staggered" or m.get("label") != p["label"]):
        raise ValueError("unverified overload arguments differ from case")
    expected_config = {k: p[k] for k in ("size", "window", "callers", "seconds", "warmup")}
    expected_config.update(rate_bytes_per_second=p["rate"], deadline_seconds=30,
                           observer_enabled=not p["observer_off"], queue_capacity=16384,
                           max_lateness_seconds=30, observer_prepare_seconds=5,
                           warmup_drain_seconds=10, arrival_alignment="staggered")
    if (config != expected_config or m.get("stream_config") != config
            or any(n.get(k) != config[k] for k in config if k not in ("seconds", "warmup", "rate_bytes_per_second"))
            or n.get("steps") != [{"seconds": p["seconds"], "rate_bytes_per_second": p["rate"]}]):
        raise ValueError("unverified overload stream configuration differs from case")
    tent, nics = a["tent-config"], p["nic"].split(",")
    if (p["nic"] != MODES[case["mode"]] or (p["size"], p["window"]) != (case["size"], case["q"])
            or tent.get("topology", {}).get("rdma_whitelist") != nics
            or sorted(nic["name"] for nic in a["topology"]["nics"]) != sorted(nics)
            or tent.get("policy") != [{"name": "test_rdma", "segment_type": "memory",
                                       "devices": nics, "transports": ["rdma"]}]
            or tent.get("transports", {}).get("rdma", {}).get("enable") is not True
            or tent["transports"]["rdma"].get("enable_smart_scheduling") is not (not p["rr"])):
        raise ValueError("unverified overload topology/policy differs from case")
    fingerprints = {}
    for name in ("tent_library", "stream_library"):
        expected = case["context"][name + "_sha256"]
        fingerprints[name] = hashlib.sha256(Path(p[name]).read_bytes()).hexdigest()
        if expected is None or fingerprints[name] != expected:
            raise ValueError("unverified overload library file hash differs from case")
    start, end = n.get("measurement_start_ns"), n.get("measurement_end_ns")
    if type(start) is not int or type(end) is not int or not 0 < end - start <= round(p["seconds"] * 1e9):
        raise ValueError("unverified overload lacks bounded measurement interval")
    with (root / "requests.jsonl").open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    if (any(type(r.get("request_id")) is not int or r["request_id"] <= 0 or r.get("bytes") != p["size"]
            or r.get("phase") not in ("warmup", "measurement")
            or r.get("status") not in ("success", "failure") for r in records)
            or len({r["request_id"] for r in records}) != len(records)):
        raise ValueError("unverified overload records lack unique classified terminal states")
    for counts, selected in ((n, records), (n.get("total", {}), records),
                             *((n.get(phase, {}), [r for r in records if r["phase"] == phase])
                               for phase in ("measurement", "warmup"))):
        if (any(type(counts.get(k)) is not int or counts[k] < 0 for k in ("accepted", "success", "failure", "pending"))
                or counts["pending"] != 0 or counts["accepted"] != counts["success"] + counts["failure"]
                or counts["accepted"] != len(selected)
                or any(counts[k] != sum(r["status"] == k for r in selected) for k in ("success", "failure"))):
            raise ValueError("unverified overload top/total/measurement/warmup records do not conserve")
    return {"status": "completed_overload_unverified", "caseHash": case_hash(case),
            "normal_successful_latency": False, "data_verified": None, "native_accounting": n,
            "library_file_sha256": fingerprints,
            "integrity_scope": "unique terminal records conserved; observer complete; receiver checks empty; "
                              "payload/guards UNVERIFIED; library hashes are current files, not load-time attestation",
            "latency_interpretation": "no normal P99 or verified goodput; overload records only; "
                                      "unsubmitted requests have no service latency; accepted records exclude "
                                      "unaccepted/stopped input; measurement bounds do not prove full-duration input; "
                                      "summary/windows are not reconstructed"}


def freeze_capacity(cases, outcomes):
    """Build capacity.json from all 81 successful calibration outcomes."""
    if len(cases) != 81 or len(outcomes) != 81:
        raise ValueError("capacity requires all 81 calibration cases")
    scans = {str(size): {mode: {str(q): [] for q in QS} for mode in MODES} for size in SIZES}
    for case in cases:
        outcome = outcomes[case_hash(case)]
        if outcome["status"] != "success":
            raise ValueError("cannot freeze capacity with failed or incomplete cases")
        goodput = read_goodput(outcome["runPath"], case)
        if not math.isfinite(goodput) or goodput <= 0:
            raise ValueError("capacity samples must have positive finite goodput")
        scans[str(case["size"])][case["mode"]][str(case["q"])].append({
            "repeat": case["repeat"], "caseHash": case_hash(case),
            "runPath": outcome["runPath"], "goodput_bytes_per_second": goodput})
    sizes = {}
    for size, modes in scans.items():
        medians = {}
        for mode, points in modes.items():
            medians[mode] = {}
            for q, samples in points.items():
                if {s["repeat"] for s in samples} != {1, 2, 3} or len(samples) != 3:
                    raise ValueError("capacity requires exactly repeats 1/2/3 at every point")
                medians[mode][q] = statistics.median(s["goodput_bytes_per_second"] for s in samples)
        d0 = medians["D0"]
        best = max(d0.values())
        chosen_q = min(q for q in QS if d0[str(q)] >= best * 0.95)
        growth = d0["128"] / d0["64"] - 1
        plateau = d0["128"] <= d0["64"] * 1.05
        sizes[size] = {
            "q": chosen_q, "fastest_q": min(QS, key=lambda q: (-d0[str(q)], q)),
            "reference_bytes_per_second": d0[str(chosen_q)],
            "mode_capacity_bytes_per_second": {mode: values[str(chosen_q)] for mode, values in medians.items()},
            "saturation_confirmed": plateau,
            "saturation_status": "plateau_observed_in_scan" if plateau else "not_confirmed",
            "q128_vs_q64_growth_fraction": growth, "medians_bytes_per_second": medians,
            "samples": modes,
        }
    return {"schema_version": 1, "context": cases[0]["context"],
            "calibration_case_hashes": sorted(case_hash(case) for case in cases),
            "selection": "smallest Q at >=95% of best D0 three-repeat median; all modes use that Q",
            "plateau_rule": "Q128 median exceeds Q64 by >5% => saturation not confirmed",
            "units": "bytes_per_second", "sizes": sizes}


def load_capacity(path, args):
    capacity = _read(path)
    frozen_context, current_context = capacity.get("context"), _context(args)
    # Reporting-window selection does not change the measured capacity reference.
    if (capacity.get("schema_version") != 1 or not isinstance(frozen_context, dict)
            or {k: v for k, v in frozen_context.items() if k != "latency_window_ms"}
            != {k: v for k, v in current_context.items() if k != "latency_window_ms"}):
        raise ValueError("capacity schema/driver/environment parameters differ; use matching calibration")
    for size in SIZES:
        entry = capacity.get("sizes", {}).get(str(size), {})
        if entry.get("q") not in QS or type(entry.get("saturation_confirmed")) is not bool:
            raise ValueError(f"capacity missing valid Q/saturation state for size {size}")
        capacities = entry.get("mode_capacity_bytes_per_second", {})
        for value in [entry.get("reference_bytes_per_second"), *(capacities.get(mode) for mode in MODES)]:
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"capacity missing positive finite measured goodput for size {size}")
        if entry["reference_bytes_per_second"] != capacities["D0"]:
            raise ValueError("shared D0 reference differs from selected-Q capacity")
    return capacity



def _unexpected_overload_case(case, exit_code):
    p = case["parameters"]
    rate, cap = p.get("rate"), case.get("mode_capacity")
    return (case["phase"] == "baseline" and exit_code == 1
            and case.get("offered_exceeds_mode_capacity") is False and p.get("steps") is None
            and type(rate) in (int, float) and type(cap) in (int, float)
            and math.isfinite(rate) and math.isfinite(cap) and 0 < rate <= cap)


def _review_unexpected_overload(case, attempt, output_root):
    """Keep failed status/error; never trust a persisted collection_allowed flag."""
    review = {"allowed": False, "reviewed_at": time.time(),
              "policy": "collect-unexpected-overload-v1"}
    attempt["collection_review"] = review
    try:
        if (attempt["status"] != "failed" or not _unexpected_overload_case(case, attempt["exitCode"])
                or attempt.get("outputError") or not attempt.get("finishedAt")):
            raise ValueError("not a finished below-capacity fixed-rate rc1 failure")
        root = _outside_repo(attempt["runPath"])
        log = Path(attempt["logPath"])
        if (not Path(attempt["runPath"]).is_absolute() or (output_root / "runs").resolve() not in root.parents
                or not log.is_absolute() or not log.is_file()
                or (output_root / case["phase"] / case_hash(case)).resolve() not in log.resolve().parents):
            raise ValueError("failed attempt lacks preserved in-suite OUTPUT/log evidence")
        manifest = _read(root / "manifest.json")
        verified = manifest.get("data_verified")
        if verified is True:
            if attempt.get("nativeDone") is not True:
                raise ValueError("verified unexpected overload lacks NATIVE_DONE")
            checks = _read(root / "correctness.json").get("checks")
            if not isinstance(checks, list) or not checks or any(c.get("passed") is not True for c in checks):
                raise ValueError("verified unexpected overload contains empty/failed checks")
            evidence = read_completed_overload(root, case, attempt["exitCode"], allow_unexpected=True)
        elif "data_verified" in manifest and verified is None:
            evidence = read_completed_overload_unverified(root, case, attempt["exitCode"], allow_unexpected=True)
        else:
            raise ValueError("unexpected overload has invalid content-verification evidence")
        native = evidence["native_accounting"]
        if native.get("stop_reason") not in ("queue_capacity_exceeded", "max_lateness_exceeded"):
            raise ValueError("unexpected overload stop_reason is outside the controlled-stop whitelist")
        p = case["parameters"]
        if verified is True:
            epochs = native.get("final_epochs")
            if (not isinstance(epochs, list) or len(epochs) != p["window"]
                    or any(e is not None and (type(e) is not int or e < 0) for e in epochs)):
                raise ValueError("unexpected overload has invalid final_epochs")
            expected_slots = {slot: epoch for slot, epoch in enumerate(epochs) if epoch is not None}
            seen = {}
            for check in checks:
                slot, epoch, digest = check.get("slot"), check.get("epoch"), check.get("sha256")
                if (type(slot) is not int or type(epoch) is not int or slot in seen
                        or check.get("passed") is not True or check.get("guard_ok") is not True
                        or check.get("bytes") != p["size"] or not isinstance(digest, str)
                        or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                        or digest != check.get("expected_sha256")):
                    raise ValueError("unexpected overload has duplicate/invalid/failed payload or guard checks")
                seen[slot] = epoch
            if seen != expected_slots:
                raise ValueError("unexpected overload checks do not exactly cover non-null final_epochs")
        if any(native.get(k) != native.get("total", {}).get(k) for k in ("accepted", "success", "failure", "pending")):
            raise ValueError("unexpected overload top/total accounting differs")
        if (native.get("start_missed") is not False or native.get("failure_reason") is not None
                or native.get("outstanding_slots") != []):
            raise ValueError("unexpected overload has unresolved native failure evidence")
        config = _read(root / "stream-config.json")
        expected_config = {k: p[k] for k in ("size", "window", "callers", "seconds", "warmup")}
        expected_config.update(rate_bytes_per_second=p["rate"], deadline_seconds=30,
                               observer_enabled=not p["observer_off"], queue_capacity=16384,
                               max_lateness_seconds=30, observer_prepare_seconds=5,
                               warmup_drain_seconds=10, arrival_alignment="staggered")
        arguments = _read(root / "arguments.json")
        if (config != expected_config or manifest.get("stream_config") != config
                or any(native.get(k) != config[k] for k in config if k not in ("seconds", "warmup", "rate_bytes_per_second"))
                or native.get("steps") != [{"seconds": p["seconds"], "rate_bytes_per_second": p["rate"]}]
                or arguments.get("gate_batch") is not False or arguments.get("arrival_alignment") != "staggered"):
            raise ValueError("unexpected overload full stream configuration differs from the extended case")
        tent, nics = _read(root / "tent-config.json"), p["nic"].split(",")
        transports = tent.get("transports", {})
        if (p["nic"] != MODES[case["mode"]] or (p["size"], p["window"]) != (case["size"], case["q"])
                or sorted(n["name"] for n in _read(root / "topology.json")["nics"]) != sorted(nics)
                or tent.get("topology", {}).get("rdma_whitelist") != nics
                or tent.get("policy") != [{"name": "test_rdma", "segment_type": "memory",
                                           "devices": nics, "transports": ["rdma"]}]
                or tent.get("enable_auto_failover_on_poll") is not False
                or set(transports) != {"rdma", "tcp", "hp_tcp", "shm", "nvlink", "mnnvl", "gds", "io_uring", "ub", "mpcomm", "tpu"}
                or any(v.get("enable") is not (k == "rdma") for k, v in transports.items())
                or transports["rdma"].get("enable_smart_scheduling") is not (not p["rr"])):
            raise ValueError("unexpected overload topology/RDMA-only policy differs from case")
        hashes = {str(log): hashlib.sha256(log.read_bytes()).hexdigest()}
        for name in ("tent_library", "stream_library"):
            digest = hashlib.sha256(Path(case["parameters"][name]).read_bytes()).hexdigest()
            if digest != case["context"].get(name + "_sha256"):
                raise ValueError("unexpected overload current library hash differs from case")
            hashes[name] = digest
        names = ["manifest", "stream-summary", "correctness", "failure", "observer",
                 "arguments", "stream-config", "tent-config", "topology"]
        if verified is True:
            names += ["summary", "windows"]
        paths = [root / (n + ".json") for n in names] + [root / "requests.jsonl"]
        if (root / "unsubmitted-or-unresolved.json").exists():
            paths.append(root / "unsubmitted-or-unresolved.json")
        hashes.update({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
        analysis = {"classification": "unexpected_controlled_overload", "status": "failed",
                    "caseHash": case_hash(case), "attempt": attempt["attempt"],
                    "exitCode": attempt["exitCode"], "nativeDone": attempt.get("nativeDone"),
                    "original_error": attempt.get("error"), "data_verified": verified,
                    "normal_successful_latency": False, "policy": review["policy"],
                    "evidence_sha256": hashes,
                    "overload_evidence": {k: v for k, v in evidence.items() if k not in ("status", "caseHash")},
                    "scope": "failed sample retained; collection only; never normal goodput/P99 or calibration"}
        path = root / "unexpected-overload-analysis.json"
        if path.exists():
            if _read(path) != analysis:
                raise ValueError("unexpected overload evidence changed since previous review")
        else:
            _save(path, analysis, exclusive=True)
        review.update(allowed=True, classification=analysis["classification"], analysis_path=str(path))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        review["error"] = str(error)
    return review["allowed"]


def _collection_report(output_root, cases, outcomes, complete):
    values = list(outcomes.values())
    report = {"policy": "collect-unexpected-overload-v1", "complete": complete,
              "planned": len(cases), "collected": len(values),
              "failed_count": sum(v["status"] == "failed" for v in values),
              "normal_success_count": sum(v["status"] == "success" for v in values),
              "expected_overload_count": sum(v["status"] in ("completed_overload", "completed_overload_unverified") for v in values),
              "unexpected_overload_count": sum(v["status"] == "failed" and v.get("collection_review", {}).get("allowed") is True for v in values),
              "planned_case_hashes": [case_hash(c) for c in cases],
              "outcomes": {k: {f: v.get(f) for f in ("status", "error", "attempt", "runPath", "collection_review")} for k, v in outcomes.items()}}
    _save(output_root / "collection-report.json", report)
    return report


def run_case(case, output_root, retry_failed=False, collect_unexpected_overload=False):
    """Execute or resume one case; retain every attempt, including failures."""
    if collect_unexpected_overload and (case["phase"] != "baseline" or retry_failed):
        raise ValueError("collection requires baseline without --retry-failed")
    identifier = case_hash(case)
    directory = _outside_repo(output_root / case["phase"] / identifier)
    state_path = directory / "state.json"
    state = _read(state_path) if state_path.exists() else {"caseHash": identifier, "case": case, "attempts": []}
    if state["case"] != case or state["caseHash"] != identifier:
        raise ValueError(f"case identity mismatch: {state_path}")
    if state["attempts"]:
        previous = state["attempts"][-1]
        if collect_unexpected_overload and previous["status"] == "failed":
            _review_unexpected_overload(case, previous, output_root)
            _save(state_path, state)
            print("REVIEW_FAILED", case["parameters"]["label"], previous["collection_review"], flush=True)
            return previous
        if previous["status"] in ("success", "completed_overload", "completed_overload_unverified"):
            if previous["status"] == "completed_overload_unverified":
                analysis = read_completed_overload_unverified(previous["runPath"], case, previous["exitCode"])
                if (not Path(previous["logPath"]).is_file()
                        or _read(Path(previous["runPath"]) / "overload-analysis.json") != analysis):
                    raise ValueError("unverified overload lacks preserved log or matching analysis")
            elif previous["status"] == "completed_overload":
                if previous.get("nativeDone") is not True or not Path(previous["logPath"]).is_file():
                    raise ValueError("completed overload lacks its completion marker or preserved sender log")
                read_completed_overload(previous["runPath"], case, previous["exitCode"])
            else:
                read_goodput(previous["runPath"], case)
            print("SKIP", case["parameters"]["label"], identifier, flush=True)
            return previous
        if not retry_failed:
            raise ValueError(f"{state_path}: previous attempt is {previous['status']}; inspect it before --retry-failed")
        if previous["status"] == "running":
            previous.update(status="interrupted", error="previous orchestrator did not record completion")
    directory.mkdir(parents=True, exist_ok=True)
    attempt = {"attempt": len(state["attempts"]) + 1, "status": "running",
               "startedAt": time.time(), "exitCode": None, "runPath": None,
               "nativeDone": False, "command": native_command(case)}
    attempt["logPath"] = str(directory / f"attempt-{attempt['attempt']:03d}.log")
    state["attempts"].append(attempt)
    _save(state_path, state)
    print("RUN", case["parameters"]["label"], identifier, flush=True)
    process = None
    try:
        with Path(attempt["logPath"]).open("x", encoding="utf-8") as log:
            process = subprocess.Popen(attempt["command"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, errors="replace", bufsize=1)
            attempt["pid"] = process.pid
            _save(state_path, state)
            for line in process.stdout:
                log.write(line)
                log.flush()
                if line.startswith("OUTPUT "):
                    path = line[len("OUTPUT "):].strip()
                    if attempt["runPath"] is not None and attempt["runPath"] != path:
                        attempt["outputError"] = "multiple different OUTPUT paths"
                    attempt["runPath"] = path
                    _save(state_path, state)
                elif line.startswith("NATIVE_DONE "):
                    attempt["nativeDone"] = True
            attempt["exitCode"] = process.wait()
        if not attempt["runPath"]:
            raise ValueError("native sender OUTPUT marker is missing")
        if attempt.get("outputError"):
            raise ValueError(attempt["outputError"])
        run_path = _outside_repo(attempt["runPath"])
        if not Path(attempt["runPath"]).is_absolute() or (output_root / "runs").resolve() not in run_path.parents:
            raise ValueError("OUTPUT is not an absolute path under this suite's runs directory")
        if (case["phase"] == "baseline" and case.get("offered_exceeds_mode_capacity") is True
                and attempt["exitCode"] in (0, 1) and _read(run_path / "manifest.json").get("native_return_code") == 1):
            if _read(run_path / "manifest.json").get("data_verified") is None:
                analysis = read_completed_overload_unverified(run_path, case, attempt["exitCode"])
                _save(run_path / "overload-analysis.json", analysis)
                attempt.update(analysis)
            else:
                if not attempt["nativeDone"]:
                    raise ValueError("verified overload lacks NATIVE_DONE marker")
                attempt.update(read_completed_overload(run_path, case, attempt["exitCode"]), status="completed_overload")
        else:
            if attempt["exitCode"] != 0 or not attempt["nativeDone"]:
                raise ValueError("native sender failed outside the validated expected-overload exception")
            attempt["goodput_bytes_per_second"] = read_goodput(run_path, case)
            attempt["status"] = "success"
    except KeyboardInterrupt:
        attempt.update(status="interrupted", error="orchestrator interrupted")
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        attempt.update(status="failed", error=str(error))
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            attempt["exitCode"] = process.returncode
            if process.stdout is not None:
                process.stdout.close()
        attempt["finishedAt"] = time.time()
        _save(state_path, state)
    if collect_unexpected_overload and attempt["status"] == "failed":
        _review_unexpected_overload(case, attempt, output_root)
        _save(state_path, state)
    print(attempt["status"].upper(), case["parameters"]["label"], attempt["runPath"], flush=True)
    return attempt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", choices=("pilot", "calibrate", "baseline", "dynamic", "local"), required=True)
    parser.add_argument("--output-root", type=Path, default=BASE / "suites")
    parser.add_argument("--capacity", type=Path, help="default: OUTPUT_ROOT/capacity.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--collect-unexpected-overload", action="store_true",
                        help="baseline only: retain audited unexpected overloads as failed and continue collection")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--seconds", type=float, default=60,
                        help="measurement seconds; baseline uses max(this value, its frozen size minimum)")
    parser.add_argument("--warmup", type=float, default=10)
    parser.add_argument("--peer", default="10.0.1.251")
    parser.add_argument("--tent-library", default=str(BASE / "build-v2/libtent_shared.so"))
    parser.add_argument("--stream-library", default=str(BASE / "build-v2/stream_native.so"))
    parser.add_argument("--latency-window-ms", type=int, choices=(250, 1000, 5000), default=1000,
                        help="reporting window; baseline uses size-specific windows unless --uniform-latency-window is set")
    parser.add_argument("--uniform-latency-window", action="store_true",
                        help="explicitly use --latency-window-ms for every baseline size")
    args = parser.parse_args(argv)
    try:
        if args.collect_unexpected_overload and (args.phase != "baseline" or args.retry_failed):
            raise ValueError("--collect-unexpected-overload requires baseline without --retry-failed")
        if args.repetitions < 1 or not 0 < args.seconds <= 600 or not 0 <= args.warmup <= 60:
            raise ValueError("require repetitions >=1, 0<seconds<=600, 0<=warmup<=60")
        args.output_root = _outside_repo(args.output_root)
        _outside_repo(args.output_root / "runs")
        capacity_path = args.capacity.expanduser().resolve() if args.capacity else args.output_root / "capacity.json"
        capacity = load_capacity(capacity_path, args) if args.phase in ("baseline", "dynamic", "local") else None
        cases = generate_cases(args, capacity)
        plan = {"phase": args.phase, "case_count": len(cases), "shuffle_seed": SHUFFLE_SEED,
                "output_root": str(args.output_root), "executable": args.phase not in ("dynamic", "local"),
                "cases": [{"caseHash": case_hash(case), "case": case,
                           "command": native_command(case) if "parameters" in case else None} for case in cases]}
        if args.dry_run or not plan["executable"]:
            print(json.dumps(plan, indent=2, allow_nan=False))
            if not args.dry_run and not plan["executable"]:
                print("proposal-only phase; no experiments launched", file=sys.stderr)
                return 2
            return 0
        if args.phase == "calibrate":
            _outside_repo(capacity_path)
        args.output_root.mkdir(parents=True, exist_ok=True)
        with (args.output_root / ".suite.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("another suite is already running under this output root")
            if args.phase == "calibrate" and capacity_path.exists():
                frozen = _read(capacity_path)
                if frozen.get("calibration_case_hashes") != sorted(case_hash(case) for case in cases):
                    raise ValueError("capacity is frozen for different cases; use a separate output root")
            outcomes = {}
            if args.collect_unexpected_overload:
                _collection_report(args.output_root, cases, outcomes, False)
            for case in cases:
                result = run_case(case, args.output_root, args.retry_failed, args.collect_unexpected_overload)
                outcomes[case_hash(case)] = result
                if args.collect_unexpected_overload:
                    _collection_report(args.output_root, cases, outcomes, False)
                allowed = ("success", "completed_overload", "completed_overload_unverified") if args.phase == "baseline" else ("success",)
                collected_failure = (args.collect_unexpected_overload and result["status"] == "failed"
                                     and result.get("collection_review", {}).get("allowed") is True)
                if result["status"] not in allowed and not collected_failure:
                    print("suite stopped; failure retained:", result.get("error"), file=sys.stderr)
                    return 1
            if args.collect_unexpected_overload:
                report = _collection_report(args.output_root, cases, outcomes, True)
                print("COLLECTION_COMPLETE", "failed_count", report["failed_count"], flush=True)
                return 1 if report["failed_count"] else 0
            if args.phase == "calibrate":
                frozen = freeze_capacity(cases, outcomes)
                if capacity_path.exists():
                    if _read(capacity_path) != frozen:
                        raise ValueError("existing frozen capacity differs; refusing to overwrite")
                else:
                    _save(capacity_path, frozen, exclusive=True)
                print("CAPACITY", capacity_path, flush=True)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
