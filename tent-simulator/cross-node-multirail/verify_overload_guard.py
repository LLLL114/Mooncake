#!/usr/bin/env python3
"""SSH-only structural fixtures; NEVER experimental results or payload verification.

Imports the selected runner and calls only read_completed_overload_unverified.
Synthetic library files are hashed, never loaded. Every invocation keeps its own
fixtures and report below an explicitly supplied server validation directory.
"""
import sys

sys.dont_write_bytecode = True

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile


VALIDATION = Path("/root/mooncake-tent-multirdma-output/cross-node-multirail/validation")
DRAIN_SECONDS = {"run_suite": 5, "run_suite_extended": 10}
SCOPE = "STRUCTURAL UNIT TEST ONLY; synthetic artifacts; not real run or CRC/payload verification"


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def fixture(root, drain):
    root.mkdir()
    context = {}
    for name in ("tent_library", "stream_library"):
        path = root / (name + ".synthetic.bin")
        content = (SCOPE + "\n" + name + "\n").encode()
        path.write_bytes(content)
        context[name] = str(path)
        context[name + "_sha256"] = hashlib.sha256(content).hexdigest()
    p = dict(nic="erdma_0", peer="192.0.2.1", size=65536, window=2, callers=1,
             seconds=60.0, warmup=1.0, rate=2000000.0, steps=None,
             label="STRUCTURAL_ONLY", rr=False, observer_off=False, alpha=None,
             workers=None, jitter=None, default_gbps=None, config_override=None,
             latency_window_ms=1000, output_root=str(root),
             tent_library=context["tent_library"], stream_library=context["stream_library"])
    case = dict(schema_version=1, phase="baseline", mode="S0", size=65536, q=2,
                repeat=1, load="90pct", parameters=p, context=context,
                capacity_reference=None, mode_capacity=1000000.0,
                offered_exceeds_mode_capacity=True)
    config = dict(size=65536, window=2, callers=1, seconds=60.0, warmup=1.0,
                  rate_bytes_per_second=2000000.0, deadline_seconds=30,
                  observer_enabled=True, queue_capacity=16384, max_lateness_seconds=30,
                  observer_prepare_seconds=5, warmup_drain_seconds=drain,
                  arrival_alignment="staggered")
    start = (5 + 1 + drain) * 1000000000
    native = dict(accepted=5, success=2, failure=3, pending=0,
                  total=dict(accepted=5, success=2, failure=3, pending=0),
                  measurement=dict(accepted=4, success=1, failure=3, pending=0),
                  warmup=dict(accepted=1, success=1, failure=0, pending=0),
                  return_code=1, final_epochs=[None, None], overflow=True,
                  input_stopped_early=True, drained=True, requests_output_complete=True,
                  watchdog_required=False, outstanding_batches=0, outstanding_slots=[],
                  start_missed=False, failure_reason=None,
                  measurement_start_ns=start, measurement_end_ns=start + 60000000000,
                  steps=[dict(seconds=60.0, rate_bytes_per_second=2000000.0)])
    native.update({k: v for k, v in config.items()
                   if k not in ("seconds", "warmup", "rate_bytes_per_second")})
    records = []
    for identity, phase, status, slot in ((1, "warmup", "success", 0),
                                         (2, "measurement", "success", 1),
                                         (3, "measurement", "failure", 0),
                                         (4, "measurement", "failure", 1),
                                         (5, "measurement", "failure", None)):
        planned = 5100000000 if phase == "warmup" else start + identity * 1000
        records.append(dict(request_id=identity, epoch=identity, flow_id=0,
                            phase=phase, status=status, bytes=65536, source_slot=slot,
                            planned_ns=planned, submitted_ns=planned + 10 if slot is not None else None,
                            finished_ns=planned + 100, cancel_requested=status == "failure"))
    artifacts = {"stream-summary": native, "stream-config": config,
                 "arguments": dict(p, gate_batch=False, arrival_alignment="staggered"),
                 "correctness": dict(passed=False, checks=[]),
                 "failure": dict(type="RuntimeError", pending_unsafe=False,
                                 error="receiver final full payload/guard mismatch"),
                 "observer": dict(incomplete=False),
                 "tent-config": {"topology": {"rdma_whitelist": ["erdma_0"]},
                                 "policy": [{"name": "test_rdma", "segment_type": "memory",
                                             "devices": ["erdma_0"], "transports": ["rdma"]}],
                                 "transports": {"rdma": {"enable": True, "enable_smart_scheduling": True}}},
                 "topology": {"nics": [{"name": "erdma_0"}]}}
    return dict(case=case, artifacts=artifacts, records=records, exit_code=1, data_verified=None)


def write_fixture(root, f):
    a = f["artifacts"]
    # Mirror mutated native evidence: counting negatives must reach conservation,
    # rather than being rejected merely for stale checkpoint/native disagreement.
    a["manifest"] = dict(a["stream-summary"], native_return_code=a["stream-summary"]["return_code"],
                         data_verified=f["data_verified"], label=f["case"]["parameters"]["label"],
                         stream_config=a["stream-config"])
    for name, value in a.items():
        save(root / (name + ".json"), value)
    save(root / "case.json", f["case"])
    (root / "requests.jsonl").write_text(
        "".join(json.dumps(r, allow_nan=False) + "\n" for r in f["records"]), encoding="utf-8")
    (root / "STRUCTURAL_ONLY.txt").write_text(SCOPE + "\n", encoding="utf-8")


def change(f, path, value):
    target = f
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", default="run_suite", help="run_suite or run_suite_extended, or its .py path")
    parser.add_argument("--fixture-root", required=True, type=Path,
                        help="explicit absolute parent below the server validation directory; never runs")
    args = parser.parse_args()
    parent = args.fixture_root.resolve()
    if not args.fixture_root.is_absolute() or VALIDATION not in parent.parents:
        parser.error(f"--fixture-root must resolve strictly below {VALIDATION}")
    runner_path = Path(args.runner)
    if args.runner in DRAIN_SECONDS:
        runner_path = Path(__file__).resolve().parent / (args.runner + ".py")
    runner_path = runner_path.resolve()
    if runner_path.stem not in DRAIN_SECONDS or runner_path.suffix != ".py" or not runner_path.is_file():
        parser.error("--runner must select an existing run_suite.py or run_suite_extended.py")
    drain = DRAIN_SECONDS[runner_path.stem]
    spec = importlib.util.spec_from_file_location("_overload_guard_runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    guard = runner.read_completed_overload_unverified
    parent.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix=runner_path.stem + "-", dir=parent))
    print(SCOPE, flush=True)
    print(f"RUNNER {runner_path} warmup_drain_seconds={drain}\nFIXTURES {output}", flush=True)
    # Expected message fragments ensure a crash or unrelated early rejection
    # cannot count as a passing negative fixture.
    cases = [("valid_empty_checks_all_null", None, None, None),
             ("nonempty_failed_checks", ("artifacts", "correctness", "checks"), [{"passed": False}], "empty-checks"),
             ("nonnull_epoch", ("artifacts", "stream-summary", "final_epochs", 0), 3, "empty-checks"),
             ("duplicate_records", ("records", 1, "request_id"), 1, "unique classified terminal"),
             ("missing_records", ("records",), "drop-last", "do not conserve")]
    for scope in ("top", "total", "measurement", "warmup"):
        path = ("artifacts", "stream-summary") + (() if scope == "top" else (scope,))
        cases.append((scope + "_counts", path + ("accepted",), 99, "do not conserve"))
    for name, value in (("pending", 1), ("watchdog_required", True), ("drained", False),
                        ("requests_output_complete", False), ("outstanding_batches", 1)):
        cases.append((name, ("artifacts", "stream-summary", name), value,
                      "do not conserve" if name == "pending" else "complete-output evidence"))
    cases.extend([
        ("observer_incomplete", ("artifacts", "observer", "incomplete"), True, "complete-output evidence"),
        ("wrong_failure", ("artifacts", "failure", "error"), "unrelated failure", "empty-checks"),
        ("wrong_failure_type", ("artifacts", "failure", "type"), "ValueError", "empty-checks"),
        ("pending_unsafe", ("artifacts", "failure", "pending_unsafe"), True, "empty-checks"),
        ("arguments", ("artifacts", "arguments", "peer"), "192.0.2.2", "arguments differ"),
        ("stream_config", ("artifacts", "stream-config", "rate_bytes_per_second"), 1, "stream configuration"),
        ("wrong_drain_contract", ("artifacts", "stream-config", "warmup_drain_seconds"), 10 if drain == 5 else 5, "stream configuration"),
        ("tent_config", ("artifacts", "tent-config", "topology", "rdma_whitelist"), ["erdma_1"], "topology/policy"),
        ("actual_topology", ("artifacts", "topology", "nics"), [{"name": "erdma_1"}], "topology/policy"),
        ("tent_library_hash", None, "tent_library", "library file hash"),
        ("stream_library_hash", None, "stream_library", "library file hash"),
        ("nonoverload_flag", ("case", "offered_exceeds_mode_capacity"), False, "not an expected"),
        ("rate_equal_capacity", ("case", "parameters", "rate"), 1000000.0, "not an expected"),
        ("rate_below_capacity", ("case", "parameters", "rate"), 500000.0, "not an expected"),
        ("calibrate", ("case", "phase"), "calibrate", "not an expected"),
        ("sender_rc", ("exit_code",), 0, "not an expected"),
        ("native_rc", ("artifacts", "stream-summary", "return_code"), -2, "checkpoint/native"),
        ("verified_claim", ("data_verified",), True, "checkpoint/native"),
        ("no_stop_evidence", None, "no-stop", "complete-output evidence"),
    ])
    results = []
    for name, path, value, rejection in cases:
        root = output / name
        f = fixture(root, drain)
        if name == "missing_records":
            f["records"].pop()
        elif name in ("tent_library_hash", "stream_library_hash"):
            Path(f["case"]["parameters"][value]).write_bytes(b"STRUCTURAL ONLY: changed current file\n")
        elif name == "no_stop_evidence":
            f["artifacts"]["stream-summary"].update(overflow=False, input_stopped_early=False)
        elif path is not None:
            change(f, path, value)
        write_fixture(root, f)
        before = snapshot(root)
        error = None
        detail = "accepted with unverified status"
        try:
            result = guard(root, f["case"], f["exit_code"])
            if rejection is not None:
                error = "invalid fixture was accepted"
            elif (result.get("status") != "completed_overload_unverified"
                  or result.get("normal_successful_latency") is not False
                  or "data_verified" not in result or result["data_verified"] is not None
                  or not result.get("integrity_scope") or not result.get("latency_interpretation")
                  or "goodput_bytes_per_second" in result):
                error = "positive fixture returned incorrect classification or missing scope limits"
        except ValueError as exc:
            detail = str(exc)
            if rejection is None or rejection not in detail:
                error = "unexpected rejection: " + detail
        except Exception as exc:
            error = f"unexpected {type(exc).__name__}: {exc}"
        if snapshot(root) != before:
            error = "validator changed raw fixture files or created derived artifacts"
        results.append(dict(name=name, passed=error is None, detail=error or detail))
        print("PASS" if error is None else "FAIL", name, error or detail, flush=True)
    report = dict(scope=SCOPE, runner=str(runner_path),
                  runner_sha256=hashlib.sha256(runner_path.read_bytes()).hexdigest(),
                  warmup_drain_seconds=drain, fixture_root=str(output), results=results,
                  passed=all(r["passed"] for r in results))
    save(output / "validation-report.json", report)
    print(f"REPORT {output / 'validation-report.json'}", flush=True)
    print(f"{sum(r['passed'] for r in results)}/{len(results)} structural checks passed; NOT real results")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
