#!/usr/bin/env python3
"""SSH-only exact patch: frozen run_suite_extended.py -> NEW sibling collection runner.

No imports/execution of either runner. All expected-overload reader bodies and
run_case's expected branch survive; only an explicitly opted-in capacity gate
and independent failed-attempt review/collection policy are added.
"""
import argparse
import difflib
import hashlib
from pathlib import Path
import sys


HELPERS = '''
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


'''


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"frozen runner anchor expected once, got {text.count(old)}: {old!r}")
    return text.replace(old, new, 1)


def patch(text):
    for anchor in ('SENDER = HERE / "native_sender_extended.py"', 'QS = (32, 64, 128)',
                   'warmup_drain_seconds=10, arrival_alignment="staggered")'):
        if text.count(anchor) != 1:
            raise ValueError(f"not the frozen extended runner contract: {anchor}")
    for name in ("read_completed_overload", "read_completed_overload_unverified"):
        text = replace_once(text, f"def {name}(run_path, case, exit_code):",
                            f"def {name}(run_path, case, exit_code, allow_unexpected=False):")
    gates = [
        '    if (case["phase"] != "baseline" or case.get("offered_exceeds_mode_capacity") is not True\n'
        '            or not case["parameters"]["rate"] > case.get("mode_capacity", float("inf"))):\n'
        '        raise ValueError("case is not an expected fixed-input baseline overload")\n',
        '    if (case["phase"] != "baseline" or case.get("offered_exceeds_mode_capacity") is not True\n'
        '            or not p["rate"] > case.get("mode_capacity", float("inf")) or exit_code != 1):\n'
        '        raise ValueError("not an expected unverified baseline overload")\n',
    ]
    for gate in gates:
        text = replace_once(text, gate, '    if not (allow_unexpected and _unexpected_overload_case(case, exit_code)):\n'
                            + "".join("    " + line for line in gate.splitlines(keepends=True)))
    text = replace_once(text, "def run_case(case, output_root, retry_failed=False):",
                        HELPERS + "def run_case(case, output_root, retry_failed=False, collect_unexpected_overload=False):")
    anchor = '    """Execute or resume one case; retain every attempt, including failures."""\n'
    text = replace_once(text, anchor, anchor + '    if collect_unexpected_overload and (case["phase"] != "baseline" or retry_failed):\n'
                        '        raise ValueError("collection requires baseline without --retry-failed")\n')
    anchor = '        previous = state["attempts"][-1]\n'
    text = replace_once(text, anchor, anchor + '        if collect_unexpected_overload and previous["status"] == "failed":\n'
                        '            _review_unexpected_overload(case, previous, output_root)\n'
                        '            _save(state_path, state)\n'
                        '            print("REVIEW_FAILED", case["parameters"]["label"], previous["collection_review"], flush=True)\n'
                        '            return previous\n')
    anchor = '    print(attempt["status"].upper(), case["parameters"]["label"], attempt["runPath"], flush=True)\n'
    text = replace_once(text, anchor, '    if collect_unexpected_overload and attempt["status"] == "failed":\n'
                        '        _review_unexpected_overload(case, attempt, output_root)\n'
                        '        _save(state_path, state)\n' + anchor)
    anchor = '    parser.add_argument("--retry-failed", action="store_true")\n'
    text = replace_once(text, anchor, anchor + '    parser.add_argument("--collect-unexpected-overload", action="store_true",\n'
                        '                        help="baseline only: retain audited unexpected overloads as failed and continue collection")\n')
    anchor = '    args = parser.parse_args(argv)\n    try:\n'
    text = replace_once(text, anchor, anchor + '        if args.collect_unexpected_overload and (args.phase != "baseline" or args.retry_failed):\n'
                        '            raise ValueError("--collect-unexpected-overload requires baseline without --retry-failed")\n')
    anchor = '            outcomes = {}\n'
    text = replace_once(text, anchor, anchor + '            if args.collect_unexpected_overload:\n'
                        '                _collection_report(args.output_root, cases, outcomes, False)\n')
    text = replace_once(text, '                result = run_case(case, args.output_root, args.retry_failed)\n',
                        '                result = run_case(case, args.output_root, args.retry_failed, args.collect_unexpected_overload)\n')
    anchor = '                outcomes[case_hash(case)] = result\n'
    text = replace_once(text, anchor, anchor + '                if args.collect_unexpected_overload:\n'
                        '                    _collection_report(args.output_root, cases, outcomes, False)\n')
    text = replace_once(text, '                if result["status"] not in allowed:\n',
                        '                collected_failure = (args.collect_unexpected_overload and result["status"] == "failed"\n'
                        '                                     and result.get("collection_review", {}).get("allowed") is True)\n'
                        '                if result["status"] not in allowed and not collected_failure:\n')
    anchor = '            if args.phase == "calibrate":\n                frozen = freeze_capacity(cases, outcomes)\n'
    text = replace_once(text, anchor, '            if args.collect_unexpected_overload:\n'
                        '                report = _collection_report(args.output_root, cases, outcomes, True)\n'
                        '                print("COLLECTION_COMPLETE", "failed_count", report["failed_count"], flush=True)\n'
                        '                return 1 if report["failed_count"] else 0\n' + anchor)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).with_name("run_suite_extended.py"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.absolute() if args.output else source.with_name("run_suite_collection.py")
    if (source.name != "run_suite_extended.py" or output.name != "run_suite_collection.py"
            or output.parent.resolve() != source.parent or output.exists() or output.is_symlink()):
        raise ValueError("require frozen run_suite_extended.py and NEW sibling run_suite_collection.py")
    raw = source.read_bytes()
    text = raw.decode("utf-8")
    result = patch(text)
    source_hash = hashlib.sha256(raw).hexdigest()
    result = replace_once(result, '#!/usr/bin/env python3\n', '#!/usr/bin/env python3\n'
                          + f'# Collection policy copy; frozen source SHA256: {source_hash}\n')
    if source.read_bytes() != raw:
        raise ValueError("frozen source changed during preparation")
    with output.open("x", encoding="utf-8") as stream:
        stream.write(result)
    print("".join(difflib.unified_diff(text.splitlines(True), result.splitlines(True),
                                      fromfile=str(source), tofile=str(output))))
    print(f"COLLECTION_RUNNER {output}; source={source_hash}; generated={hashlib.sha256(result.encode()).hexdigest()}; UNTESTED")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(f"prepare_collection_runner: {error}")
