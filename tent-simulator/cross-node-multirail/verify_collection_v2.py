#!/usr/bin/env python3
"""SSH-only synthetic fixtures; no real results, sender, RDMA or library loads.

Run alongside verify_collection_runner.py and verify_overload_guard.py. The old
79-case script already accepts --runner run_suite_collection_v2.py unchanged:
its direct checks still call the old review; run_case exercises the dispatcher.
"""
import sys
sys.dont_write_bytecode = True
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from verify_collection_runner import make_case, require
from verify_overload_guard import save, change, VALIDATION


def unstarted(f):
    p, a = f["case"]["parameters"], f["artifacts"]
    n = a["stream-summary"]
    start = n["measurement_start_ns"]
    warm_end = start - 10000000000
    warm_start = warm_end - round(p["warmup"] * 1e9)
    drain = start + 575205344
    counts = dict(accepted=2, submitted=2, success=2, failure=0, pending=0, rejected=0, backpressure=0)
    n.update(counts)
    n.update(total=dict(counts), warmup=dict(counts, start_ns=warm_start, end_ns=warm_end),
             measurement={k: 0 for k in counts}, return_code=-2,
             stop_reason="measurement_start_missed", failure_reason="measurement_start_missed",
             start_missed=True, overflow=False, input_stop_ns=None, drain_end_ns=drain,
             measurement_end_ns=start + round(p["seconds"] * 1e9), final_epochs=[1, 2])
    f["records"][:] = [dict(request_id=i, epoch=i, flow_id=0, source_slot=i - 1,
        phase="warmup", status="success", bytes=p["size"], transferred_bytes=p["size"],
        planned_ns=warm_start + i * 1000, submitted_ns=warm_start + i * 1000 + 10,
        finished_ns=drain if i == 2 else warm_start + 1100, backpressured=False)
        for i in (1, 2)]
    a["failure"]["error"] = "native stream returned -2; see saved terminal accounting"


def make_unstarted(runner, suite, name, mutation=None):
    def prepare(f):
        unstarted(f)
        if mutation:
            mutation(f)
    case, attempt, root, state = make_case(runner, suite, name, True, prepare)
    # Only our fresh synthetic fixture files are removed. Test that the new
    # reviewer needs no summary/windows and does not synthesize them afterward.
    for name in ("summary.json", "windows.json", "unsubmitted-or-unresolved.json"):
        (root / name).unlink()
    Path(attempt["logPath"]).write_text(f"STRUCTURAL FIXTURE ONLY\nOUTPUT {root}\nNATIVE_DONE {root} RC -2 VERIFIED True\n")
    return case, attempt, root, state


def routed_review(runner, case, attempt, suite, use_new):
    with patch.object(runner, "_review_unstarted_measurement", wraps=runner._review_unstarted_measurement) as new:
        with patch.object(runner, "_review_unexpected_overload", wraps=runner._review_unexpected_overload) as old:
            allowed = runner._review_collection_failure(case, attempt, suite)
    require(new.call_count == int(use_new) and old.call_count == int(not use_new), "wrong dispatcher route")
    return allowed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, type=Path)
    parser.add_argument("--fixture-root", required=True, type=Path)
    args = parser.parse_args()
    parent = args.fixture_root.resolve()
    if not args.fixture_root.is_absolute() or VALIDATION not in parent.parents:
        parser.error(f"fixture-root must resolve strictly below {VALIDATION}")
    runner_path = args.runner.resolve()
    spec = importlib.util.spec_from_file_location("collection_v2_fixture_runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    parent.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="collection-v2-", dir=parent))
    print("SYNTHETIC STRUCTURAL FIXTURES ONLY", output)
    tests = [("valid_unstarted", None, None)]
    edits = [
        ("nonzero_measurement", ("artifacts", "stream-summary", "measurement", "accepted"), 1, "measurement contains"),
        ("pending", ("artifacts", "stream-summary", "pending"), 1, "top/total/warmup"),
        ("failure_count", ("artifacts", "stream-summary", "total", "failure"), 1, "top/total/warmup"),
        ("failed_record", ("records", 1, "status"), "failure", "raw records"),
        ("measurement_record", ("records", 1, "phase"), "measurement", "raw records"),
        ("duplicate_record", ("records", 1, "request_id"), 1, "raw records"),
        ("missing_timestamp", ("records", 1, "submitted_ns"), None, "raw records"),
        ("bytes_mismatch", ("records", 1, "bytes"), 1, "raw records"),
        ("crc_failed", ("artifacts", "correctness", "passed"), False, "receiver checks"),
        ("guard_failed", ("artifacts", "correctness", "checks", 1, "guard_ok"), False, "payload or guard"),
        ("digest_mismatch", ("artifacts", "correctness", "checks", 1, "sha256"), "b" * 64, "payload or guard"),
        ("epoch_mismatch", ("artifacts", "correctness", "checks", 1, "epoch"), 999, "exactly cover"),
        ("duplicate_slot", ("artifacts", "correctness", "checks", 1, "slot"), 0, "payload or guard"),
        ("drain_before_start", ("artifacts", "stream-summary", "drain_end_ns"), 15999999999, "fixed drain10"),
        ("wrong_warmup_end", ("artifacts", "stream-summary", "warmup", "end_ns"), 6000000001, "fixed drain10"),
        ("config_drain5", ("artifacts", "stream-config", "warmup_drain_seconds"), 5, "full stream configuration"),
        ("observer_incomplete", ("artifacts", "observer", "incomplete"), True, "exact native"),
        ("watchdog", ("artifacts", "stream-summary", "watchdog_required"), True, "exact native"),
        ("wrong_failure", ("artifacts", "failure", "error"), "native_timeout", "different sender failure"),
        ("tcp_enabled", ("artifacts", "tent-config", "transports", "tcp", "enable"), True, "RDMA-only policy"),
        ("topology_mismatch", ("artifacts", "topology", "nics"), [dict(name="erdma_1")], "RDMA-only policy"),
    ]
    tests += [(name, lambda f, p=path, v=value: change(f, p, v), error) for name, path, value, error in edits]
    tests += [
        ("native_timeout", lambda f: f["artifacts"]["stream-summary"].update(
            stop_reason="native_timeout", failure_reason="native_timeout", start_missed=False), "exact native"),
        ("unknown_reason", lambda f: f["artifacts"]["stream-summary"].update(
            stop_reason="unknown", failure_reason="unknown"), "exact native"),
        ("coherent_crc_wrong_record_epoch", lambda f: (
            f["artifacts"]["stream-summary"]["final_epochs"].__setitem__(1, 999),
            f["artifacts"]["correctness"]["checks"][1].update(epoch=999)), "last successful"),
        ("missing_measurement_counter", lambda f: f["artifacts"]["stream-summary"]["measurement"].pop("submitted"), "measurement contains"),
        ("library_hash", lambda f: Path(f["case"]["parameters"]["tent_library"]).write_bytes(b"changed synthetic library"), "library hash"),
    ]
    results = []
    with patch.object(runner.subprocess, "Popen", side_effect=AssertionError("fixture attempted to run a sender")):
        for name, mutation, expected_error in tests:
            suite = output / name
            case, attempt, root, state = make_unstarted(runner, suite, name, mutation)
            identity, original = runner.case_hash(case), copy.deepcopy(attempt)
            raw = {p: p.read_bytes() for p in root.iterdir() if p.is_file()}
            allowed = runner._review_unstarted_measurement(case, attempt, suite)
            require(allowed == (expected_error is None), name + ": incorrect whitelist result")
            if expected_error:
                require(expected_error in attempt["collection_review"].get("error", ""), name + ": unrelated rejection")
            native = json.loads((root / "stream-summary.json").read_text())
            use_new = native["return_code"] == -2 and native["stop_reason"] == "measurement_start_missed"
            require(routed_review(runner, case, attempt, suite, use_new) == allowed, name + ": dispatcher changed eligibility")
            result = runner.run_case(case, suite, collect_unexpected_overload=True)
            require(result["collection_review"]["allowed"] == allowed, name + ": resume changed eligibility")
            require({k: result[k] for k in original} == original, "original failed status/error/attempt changed")
            saved = json.loads(state.read_text())
            require(len(saved["attempts"]) == 1 and saved["case"] == case
                    and saved["caseHash"] == identity == runner.case_hash(case), "resume retried/changed case")
            require(all(p.read_bytes() == data for p, data in raw.items()), "raw evidence rewritten")
            require(not (root / "summary.json").exists() and not (root / "windows.json").exists(), "invented measurement outputs")
            require((root / "unstarted-measurement-analysis.json").exists() == allowed, "incorrect analysis publication")
            if allowed:
                analysis = json.loads((root / "unstarted-measurement-analysis.json").read_text())
                require(analysis["classification"] == "measurement_not_started" and analysis["status"] == "failed"
                        and analysis["algorithm_outcome"] == "not_measured" and analysis["normal_successful_latency"] is False,
                        "unstarted measurement was interpreted as an algorithm result")
                native["measurement"]["accepted"] = 1
                manifest = json.loads((root / "manifest.json").read_text())
                manifest.update(native)
                save(root / "stream-summary.json", native)
                save(root / "manifest.json", manifest)
                result = runner.run_case(case, suite, collect_unexpected_overload=True)
                require(result["collection_review"]["allowed"] is False, "resume trusted stale analysis")
            results.append(dict(name=name, allowed=allowed, route="unstarted" if use_new else "old_overload"))
            print("PASS", name)
        suite = output / "mixed-matrix"
        cases = [make_unstarted(runner, suite, "unstarted")[0]]
        for verified in (False, True):
            case, attempt, root, _ = make_case(runner, suite, "overload-" + str(verified), verified)
            require(runner._review_unexpected_overload(case, attempt, suite), "old overload entry regressed")
            require(routed_review(runner, case, attempt, suite, False), "old overload dispatcher regressed")
            cases.append(case)
        with patch.object(runner, "generate_cases", return_value=cases), patch.object(runner, "load_capacity", return_value={}):
            rc = runner.main(["--phase", "baseline", "--collect-unexpected-overload", "--output-root", str(suite)])
        report = json.loads((suite / "collection-report.json").read_text())
        require(rc == 1 and report["complete"] is True and report["failed_count"] == 3
                and report["unstarted_measurement_count"] == 1 and report["unexpected_overload_count"] == 2
                and report["normal_success_count"] == 0, "mixed matrix classification/count/exit semantics failed")
        results.append(dict(name="mixed-matrix", failed_count=3, unstarted=1, overload=2, exit_code=rc))
    save(output / "structural-report.json", dict(scope="SYNTHETIC STRUCTURAL CHECKS ONLY; NOT REAL RESULTS",
        results=results, runner_sha256=hashlib.sha256(runner_path.read_bytes()).hexdigest()))
    print("STRUCTURAL_CHECKS_PASSED", len(results), output)


if __name__ == "__main__":
    main()
