#!/usr/bin/env python3
"""SSH-only synthetic structural fixtures, NOT experiment/CRC results.

Reuses verify_overload_guard.py's fixture builder. Tests both readers, failed
resume, unchanged case identity/raw evidence, and collection-complete/exit1.
All fixture writes stay below the explicit server validation root. Popen is
forbidden during tests; main's case generation/capacity loading are mocked.
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
from verify_overload_guard import fixture, write_fixture, save, change, VALIDATION


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def make_case(runner, suite, name, verified, mutation=None):
    root = suite / "runs" / name
    root.parent.mkdir(parents=True, exist_ok=True)
    f = fixture(root, 10)
    case, a, records = f["case"], f["artifacts"], f["records"]
    case["context"].update(python=sys.executable, sender="STRUCTURAL_ONLY_NOT_EXECUTABLE")
    p, n = case["parameters"], a["stream-summary"]
    case.update(mode_capacity=164246323.2, offered_exceeds_mode_capacity=False)
    p.update(rate=148936458.24, output_root=str(root.parent), label=name)
    a["arguments"].update(p)
    a["stream-config"]["rate_bytes_per_second"] = p["rate"]
    n["steps"][0]["rate_bytes_per_second"] = p["rate"]
    n["measurement_end_ns"] = n["measurement_start_ns"] + 1000000000
    n["stop_reason"] = "queue_capacity_exceeded"
    a["tent-config"]["enable_auto_failover_on_poll"] = False
    a["tent-config"]["transports"].update({name: dict(enable=False) for name in
        ("tcp", "hp_tcp", "shm", "nvlink", "mnnvl", "gds", "io_uring", "ub", "mpcomm", "tpu")})
    if verified:
        f["data_verified"] = True
        n["final_epochs"] = [1, 2]
        for r in records[2:]:
            r.update(source_slot=None, submitted_ns=None)
        # Synthetic digest equality exercises the guard; it is not payload verification.
        a["correctness"] = dict(passed=True, checks=[dict(passed=True, guard_ok=True, slot=slot,
            epoch=slot + 1, bytes=p["size"], sha256="a" * 64, expected_sha256="a" * 64) for slot in range(2)])
        a["failure"]["error"] = "native stream returned 1; see saved terminal accounting"
    if mutation:
        mutation(f)
    write_fixture(root, f)
    if verified:
        a["manifest"]["environment"] = dict(nic=p["nic"], tent_sha256=case["context"]["tent_library_sha256"],
                                             stream_sha256=case["context"]["stream_library_sha256"])
        save(root / "manifest.json", a["manifest"])
        start, end = n["measurement_start_ns"], n["measurement_end_ns"]
        completed = [r for r in records if r["submitted_ns"] is not None and r["finished_ns"] is not None]
        excluded = [r for r in records if r not in completed]
        payload = sum(r["bytes"] for r in completed if r["status"] == "success" and start <= r["finished_ns"] < end)
        summary = dict(measurement_start_ns=start, measurement_end_ns=end, complete_run_accounting=n,
                       throughput=dict(success_bytes=payload, bits_per_second=payload * 8e9 / (end - start)),
                       excluded_non_submitted_terminal_records=len(excluded), latency_success={},
                       latency_failure_wait={}, measurement={}, observed={}, conservation={})
        throughput = {}
        for label, width in (("50ms", 50000000), ("250ms", 250000000), ("1000ms", 1000000000)):
            throughput[label] = [dict(start_ns=left, end_ns=min(left + width, end), success_bytes=sum(
                r["bytes"] for r in completed if r["status"] == "success" and left <= r["finished_ns"] < min(left + width, end)))
                for left in range(start, end, width)]
        width = p["latency_window_ms"] * 1000000
        windows = dict(throughput=throughput, latency=dict(window_ns=width, windows=[
            dict(start_ns=left, end_ns=min(left + width, end), requests=sum(
                left <= r["planned_ns"] < min(left + width, end) for r in completed))
            for left in range(start, end, width)]))
        save(root / "summary.json", summary)
        save(root / "windows.json", windows)
        save(root / "unsubmitted-or-unresolved.json", excluded)
    identifier = runner.case_hash(case)
    state_dir = suite / case["phase"] / identifier
    state_dir.mkdir(parents=True)
    log = state_dir / "attempt-001.log"
    log.write_text(f"STRUCTURAL FIXTURE ONLY\nOUTPUT {root}\n" + (f"NATIVE_DONE {root} RC 1 VERIFIED True\n" if verified else ""))
    attempt = dict(attempt=1, status="failed", error="original failed attempt: retain this exact error",
                   runPath=str(root), logPath=str(log), exitCode=f["exit_code"], nativeDone=verified, finishedAt=1)
    state = dict(caseHash=identifier, case=case, attempts=[attempt])
    save(state_dir / "state.json", state)
    return case, attempt, root, state_dir / "state.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, type=Path)
    parser.add_argument("--fixture-root", required=True, type=Path)
    args = parser.parse_args()
    parent = args.fixture_root.resolve()
    if not args.fixture_root.is_absolute() or VALIDATION not in parent.parents:
        parser.error(f"fixture-root must resolve strictly below {VALIDATION}")
    runner_path = args.runner.resolve()
    spec = importlib.util.spec_from_file_location("collection_fixture_runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    parent.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="collection-", dir=parent))
    print("STRUCTURAL FIXTURES ONLY; NOT REAL RESULTS", output)
    tests = [("below_cap_rc1", None, True),
             ("equal_cap_rc1", lambda f: f["case"].update(mode_capacity=f["case"]["parameters"]["rate"]), True),
             ("max_lateness_rc1", lambda f: f["artifacts"]["stream-summary"].update(stop_reason="max_lateness_exceeded"), True)]
    mutations = [
        ("sender_rc0", ("exit_code",), 0),
        ("sender_rc_minus2", ("exit_code",), -2),
        ("native_rc0", ("artifacts", "stream-summary", "return_code"), 0),
        ("native_rc_minus2", ("artifacts", "stream-summary", "return_code"), -2),
        ("pending", ("artifacts", "stream-summary", "total", "pending"), 1),
        ("watchdog", ("artifacts", "stream-summary", "watchdog_required"), True),
        ("observer_incomplete", ("artifacts", "observer", "incomplete"), True),
        ("wrong_failure", ("artifacts", "failure", "error"), "unrelated error"),
        ("steps", ("case", "parameters", "steps"), "[]"),
        ("zero_rate", ("case", "parameters", "rate"), 0),
        ("zero_capacity", ("case", "mode_capacity"), 0),
        ("calibrate", ("case", "phase"), "calibrate"),
        ("duplicate_record", ("records", 1, "request_id"), 1),
        ("false_expectation_flag", ("case", "offered_exceeds_mode_capacity"), True),
    ]
    tests += [(name, lambda f, p=path, v=value: change(f, p, v), False) for name, path, value in mutations]
    tests += [("nonempty_crc_failure", lambda f: f["artifacts"]["correctness"].update(
        passed=False, checks=[dict(passed=False, guard_ok=False)]), False)]
    # Last two columns are direct-reader expectations (verified, unverified).
    # Review must reject every strict negative even when its reader accepts it.
    strict = [
        ("stop_reason_unknown", ("artifacts", "stream-summary", "stop_reason"), "other_stop", True, True),
        ("stop_reason_missing", ("artifacts", "stream-summary", "stop_reason"), None, True, True),
        ("config_deadline", ("artifacts", "stream-config", "deadline_seconds"), 31, True, False),
        ("config_drain5", ("artifacts", "stream-config", "warmup_drain_seconds"), 5, True, False),
        ("config_extra", ("artifacts", "stream-config", "unexpected_option"), True, True, False),
        ("actual_topology", ("artifacts", "topology", "nics"), [dict(name="erdma_1")], True, False),
        ("policy_transport", ("artifacts", "tent-config", "policy", 0, "transports"), ["tcp"], True, False),
        ("rdma_disabled", ("artifacts", "tent-config", "transports", "rdma", "enable"), False, True, False),
        ("smart_disabled", ("artifacts", "tent-config", "transports", "rdma", "enable_smart_scheduling"), False, True, False),
        ("tcp_enabled", ("artifacts", "tent-config", "transports", "tcp", "enable"), True, True, True),
        ("auto_failover", ("artifacts", "tent-config", "enable_auto_failover_on_poll"), True, True, True),
        ("gate_batch", ("artifacts", "arguments", "gate_batch"), True, True, False),
    ]
    crc_strict = [
        ("check_failed_despite_top_pass", ("artifacts", "correctness", "checks", 0, "passed"), False),
        ("guard_failed", ("artifacts", "correctness", "checks", 0, "guard_ok"), False),
        ("sha_mismatch", ("artifacts", "correctness", "checks", 0, "sha256"), "b" * 64),
        ("expected_sha_missing", ("artifacts", "correctness", "checks", 0, "expected_sha256"), None),
        ("sha_malformed", ("artifacts", "correctness", "checks", 0, "sha256"), "not-a-digest"),
        ("check_bytes", ("artifacts", "correctness", "checks", 0, "bytes"), 1),
        ("duplicate_slot", ("artifacts", "correctness", "checks", 1, "slot"), 0),
        ("wrong_epoch", ("artifacts", "correctness", "checks", 1, "epoch"), 999),
        ("check_for_null_epoch", ("artifacts", "stream-summary", "final_epochs", 0), None),
        ("epoch_count", ("artifacts", "stream-summary", "final_epochs"), [1]),
        ("empty_checks", ("artifacts", "correctness", "checks"), []),
    ]
    passed, decisions = [], []
    # This is a regression sentinel, not a simulated sender: any launch is a failure.
    with patch.object(runner.subprocess, "Popen", side_effect=AssertionError("fixture attempted to launch a sender")):
        for verified in (False, True):
            branch_tests = [(name, mutation, accepted, accepted) for name, mutation, accepted in tests]
            branch_tests += [(name, lambda f, p=path, v=value: change(f, p, v), dv if verified else du, False)
                             for name, path, value, dv, du in strict]
            branch_tests.append(("missing_transport", lambda f: f["artifacts"]["tent-config"]["transports"].pop("tcp"), True, False))
            if verified:
                branch_tests += [(name, lambda f, p=path, v=value: change(f, p, v), True, False)
                                 for name, path, value in crc_strict]
                branch_tests.append(("missing_slot_check", lambda f: f["artifacts"]["correctness"]["checks"].pop(), True, False))
                branch_tests.append(("extra_slot_check", lambda f: f["artifacts"]["correctness"]["checks"].append(
                    dict(f["artifacts"]["correctness"]["checks"][0], slot=2, epoch=3)), True, False))
            for name, mutation, reader_accepts, accepted in branch_tests:
                label = ("verified-" if verified else "unverified-") + name
                suite = output / label
                case, attempt, root, state_path = make_case(runner, suite, label, verified, mutation)
                identity, original_attempt = runner.case_hash(case), copy.deepcopy(attempt)
                raw = {p: p.read_bytes() for p in root.iterdir() if p.is_file()}
                reader = runner.read_completed_overload if verified else runner.read_completed_overload_unverified
                if accepted:
                    try:
                        reader(root, case, attempt["exitCode"])
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("default reader accepted a below-capacity case")
                try:
                    reader(root, case, attempt["exitCode"], allow_unexpected=True)
                except ValueError:
                    require(not reader_accepts, label + ": direct reader unexpectedly rejected fixture")
                else:
                    require(reader_accepts, label + ": direct reader unexpectedly accepted fixture")
                allowed = runner._review_unexpected_overload(case, attempt, suite)
                require(allowed == accepted, label + ": wrong collection eligibility")
                require({k: attempt[k] for k in original_attempt} == original_attempt, "original failure fields changed")
                require(runner.case_hash(case) == identity, "case or hash changed")
                require(all(p.read_bytes() == data for p, data in raw.items()), "raw evidence rewritten")
                if accepted:
                    result = runner.run_case(case, suite, collect_unexpected_overload=True)
                    require(result["status"] == "failed" and result["error"] == original_attempt["error"], "resume rewrote failure")
                    state = json.loads(state_path.read_text())
                    require(len(state["attempts"]) == 1 and state["caseHash"] == identity and state["case"] == case, "resume retried/changed identity")
                    require((root / "unexpected-overload-analysis.json").is_file(), "missing independent analysis")
                    if not verified:
                        require(not (root / "summary.json").exists() and not (root / "windows.json").exists(), "invented derived artifacts")
                    # A persisted allowed review must not override newly damaged raw evidence.
                    corruption = json.loads((root / "observer.json").read_text())
                    corruption["incomplete"] = True
                    save(root / "observer.json", corruption)
                    result = runner.run_case(case, suite, collect_unexpected_overload=True)
                    require(result["collection_review"]["allowed"] is False and result["status"] == "failed", "resume trusted stale eligibility")
                else:
                    require(not (root / "unexpected-overload-analysis.json").exists(), "invalid fixture got acceptance analysis")
                passed.append(label)
                decisions.append(dict(name=label, direct_reader_accepted=reader_accepts, review_allowed=allowed))
                print("PASS", label, "reader_accepted", reader_accepts, "review_allowed", allowed)
            # Regression: default expected gate still accepts a genuinely above-capacity fixture.
            case, attempt, root, _ = make_case(runner, output / ("expected-" + str(verified)), "expected", verified,
                                              lambda f: f["case"].update(mode_capacity=1000000, offered_exceeds_mode_capacity=True))
            reader = runner.read_completed_overload if verified else runner.read_completed_overload_unverified
            reader(root, case, 1)
            passed.append("expected-default-" + str(verified))
        suite = output / "matrix-resume"
        cases = [make_case(runner, suite, "matrix-" + str(v), v)[0] for v in (False, True)]
        with patch.object(runner, "generate_cases", return_value=cases), patch.object(runner, "load_capacity", return_value={}):
            rc = runner.main(["--phase", "baseline", "--collect-unexpected-overload", "--output-root", str(suite)])
        report = json.loads((suite / "collection-report.json").read_text())
        require(rc == 1 and report["complete"] is True and report["failed_count"] == 2
                and report["unexpected_overload_count"] == 2 and report["normal_success_count"] == 0, "matrix did not complete with failures/exit1")
        passed.append("matrix-complete-exit1")
        for flags in (["--phase", "calibrate"], ["--phase", "baseline", "--retry-failed"]):
            try:
                runner.main([*flags, "--collect-unexpected-overload"])
            except SystemExit as error:
                require(error.code == 2, "wrong CLI rejection")
            else:
                raise AssertionError("invalid collection policy flags accepted")
        passed.append("baseline-only-no-retry")
    save(output / "structural-report.json", dict(scope="SYNTHETIC STRUCTURAL TESTS ONLY", passed=passed, decisions=decisions,
                                                 runner_sha256=hashlib.sha256(runner_path.read_bytes()).hexdigest()))
    print("STRUCTURAL_CHECKS_PASSED", len(passed), output)


if __name__ == "__main__":
    main()
