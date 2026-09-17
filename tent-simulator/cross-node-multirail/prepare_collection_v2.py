#!/usr/bin/env python3
"""SSH-only source preparation; never import/run a runner or overwrite inputs.

Pin the frozen collection runner, retain its readers and overload review, and
add only a measurement_start_missed/-2 failed-attempt whitelist. Shared strict
CRC/config/policy source blocks are copied verbatim from that pinned input.
"""
import argparse
import difflib
import hashlib
from pathlib import Path
import sys

EXPECTED_SHA256 = "7a73d896d43b266f63e0f3bf76ddee7634d0b736422eb8669e64d21b09c24439"

REVIEW = '''
def _review_unstarted_measurement(case, attempt, output_root):
    review = {"allowed": False, "reviewed_at": time.time(), "policy": "collect-unstarted-measurement-v2"}
    attempt["collection_review"] = review
    try:
        p = case["parameters"]
        if (case["phase"] != "baseline" or attempt["status"] != "failed" or attempt["exitCode"] != 1
                or attempt.get("nativeDone") is not True or attempt.get("outputError")
                or not attempt.get("finishedAt") or p.get("steps") is not None or not p["warmup"] > 0):
            raise ValueError("not a finished baseline failure with a completed warmup")
        root = _outside_repo(attempt["runPath"])
        log = Path(attempt["logPath"])
        if (not Path(attempt["runPath"]).is_absolute() or (output_root / "runs").resolve() not in root.parents
                or not log.is_absolute() or not log.is_file()
                or (output_root / case["phase"] / case_hash(case)).resolve() not in log.resolve().parents):
            raise ValueError("unstarted measurement lacks preserved in-suite OUTPUT/log evidence")
        names = ("manifest", "stream-summary", "correctness", "failure", "observer",
                 "arguments", "stream-config", "tent-config", "topology")
        a = {name: _read(root / (name + ".json")) for name in names}
        manifest, native = a["manifest"], a["stream-summary"]
        if (native.get("return_code") != -2 or manifest.get("native_return_code") != -2
                or native.get("stop_reason") != "measurement_start_missed"
                or native.get("failure_reason") != "measurement_start_missed"
                or native.get("start_missed") is not True or native.get("overflow") is not False
                or native.get("drained") is not True or native.get("requests_output_complete") is not True
                or native.get("watchdog_required") is not False or native.get("outstanding_batches") != 0
                or native.get("outstanding_slots") != [] or manifest.get("data_verified") is not True
                or _json({k: manifest[k] for k in native}) != _json(native)
                or a["observer"].get("incomplete") is not False):
            raise ValueError("unstarted measurement lacks exact native failure/drain/manifest/observer evidence")
        failure = a["failure"]
        if (failure.get("type") != "RuntimeError" or failure.get("pending_unsafe") is not False
                or failure.get("error") != "native stream returned -2; see saved terminal accounting"):
            raise ValueError("unstarted measurement has a different sender failure")
        if any(a["arguments"].get(k) != v for k, v in p.items()):
            raise ValueError("unstarted measurement sender arguments differ from case")
        start, end, drain = (native.get(k) for k in ("measurement_start_ns", "measurement_end_ns", "drain_end_ns"))
        warmup = native.get("warmup", {})
        warm_start, warm_end = warmup.get("start_ns"), warmup.get("end_ns")
        if (any(type(t) is not int or t <= 0 for t in (start, end, drain, warm_start, warm_end))
                or end - start != round(p["seconds"] * 1e9) or drain < start
                or warm_end - warm_start != round(p["warmup"] * 1e9) or start - warm_end != 10_000_000_000):
            raise ValueError("unstarted measurement lacks the fixed drain10 timeline or drain crossed start")
        with (root / "requests.jsonl").open(encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream]
        seen_ids, last_epochs = set(), {}
        if not records:
            raise ValueError("unstarted measurement has no completed warmup records")
        for r in records:
            identity, slot = r.get("request_id"), r.get("source_slot")
            times = [r.get(k) for k in ("planned_ns", "submitted_ns", "finished_ns")]
            if (type(identity) is not int or identity <= 0 or identity in seen_ids
                    or type(r.get("epoch")) is not int or r["epoch"] != identity
                    or r.get("phase") != "warmup" or r.get("status") != "success"
                    or type(r.get("bytes")) is not int or r["bytes"] != p["size"]
                    or type(slot) is not int or not 0 <= slot < p["window"]
                    or type(r.get("backpressured")) is not bool
                    or any(type(t) is not int or t <= 0 for t in times)
                    or not warm_start <= times[0] < warm_end
                    or not times[0] <= times[1] <= times[2] <= drain):
                raise ValueError("unstarted measurement has non-warmup/failed/duplicate/invalid raw records")
            seen_ids.add(identity)
            last_epochs[slot] = max(last_epochs.get(slot, 0), identity)
        expected_counts = dict(accepted=len(records), submitted=len(records), success=len(records),
                               failure=0, pending=0, rejected=0,
                               backpressure=sum(r["backpressured"] for r in records))
        for counts in (native, native.get("total", {}), warmup):
            if any(type(counts.get(k)) is not int or counts[k] != v for k, v in expected_counts.items()):
                raise ValueError("unstarted measurement top/total/warmup counts do not conserve raw records")
        measurement = native.get("measurement", {})
        if (set(measurement) != set(expected_counts)
                or any(type(v) is not int or v != 0 for v in measurement.values())):
            raise ValueError("measurement contains nonzero or incomplete counters")
        verified = True
        checks = a["correctness"].get("checks")
        if (a["correctness"].get("passed") is not True or not isinstance(checks, list) or not checks):
            raise ValueError("unstarted measurement lacks nonempty successful receiver checks")
@@CRC@@
        if expected_slots != last_epochs:
            raise ValueError("final_epochs do not match the last successful warmup record per slot")
@@CONFIG@@
        hashes = {str(log): hashlib.sha256(log.read_bytes()).hexdigest()}
        environment = manifest.get("environment", {})
        if environment.get("nic") != p["nic"]:
            raise ValueError("unstarted measurement environment NIC differs from case")
        for name, field in (("tent_library", "tent_sha256"), ("stream_library", "stream_sha256")):
            digest = hashlib.sha256(Path(p[name]).read_bytes()).hexdigest()
            if digest != case["context"].get(name + "_sha256") or environment.get(field) != digest:
                raise ValueError("unstarted measurement current/manifest library hash differs from case")
            hashes[name] = digest
        paths = [root / (n + ".json") for n in names] + [root / "requests.jsonl"]
        # Preserve any sender-produced derived files; never require or reconstruct them.
        paths += [root / n for n in ("summary.json", "windows.json", "unsubmitted-or-unresolved.json") if (root / n).exists()]
        hashes.update({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
        analysis = {"classification": "measurement_not_started", "status": "failed",
                    "caseHash": case_hash(case), "attempt": attempt["attempt"], "exitCode": attempt["exitCode"],
                    "nativeDone": attempt["nativeDone"], "original_error": attempt.get("error"),
                    "policy": review["policy"], "data_verified": True, "normal_successful_latency": False,
                    "algorithm_outcome": "not_measured", "failure_scope": "framework measurement-start timing",
                    "evidence_sha256": hashes, "native_accounting": native,
                    "scope": "only warmup completed; scheduled measurement bounds are not measurement evidence; no measurement goodput/P99"}
        path = root / "unstarted-measurement-analysis.json"
        if path.exists():
            if _read(path) != analysis:
                raise ValueError("unstarted measurement evidence changed since previous review")
        else:
            _save(path, analysis, exclusive=True)
        review.update(allowed=True, classification=analysis["classification"], analysis_path=str(path))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        review["error"] = str(error)
    return review["allowed"]


def _review_collection_failure(case, attempt, output_root):
    try:
        native = _read(Path(attempt["runPath"]) / "stream-summary.json")
    except (OSError, ValueError, KeyError, TypeError):
        return _review_unexpected_overload(case, attempt, output_root)
    if native.get("return_code") == -2 and native.get("stop_reason") == "measurement_start_missed":
        return _review_unstarted_measurement(case, attempt, output_root)
    return _review_unexpected_overload(case, attempt, output_root)


'''


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"frozen v1 anchor expected once, got {text.count(old)}: {old!r}")
    return text.replace(old, new, 1)


def block(text, start, end):
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("frozen v1 strict review block boundary mismatch")
    left, right = text.index(start), text.index(end)
    if right <= left:
        raise ValueError("frozen v1 strict review block order mismatch")
    return text[left:right].rstrip("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).with_name("run_suite_collection.py"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.absolute() if args.output else source.with_name("run_suite_collection_v2.py")
    if (source.name != "run_suite_collection.py" or output.name != "run_suite_collection_v2.py"
            or output.parent.resolve() != source.parent or output.exists() or output.is_symlink()):
        raise ValueError("require frozen collection runner and NEW sibling run_suite_collection_v2.py")
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_SHA256:
        raise ValueError("frozen collection runner SHA256 mismatch; refusing to patch")
    text = raw.decode("utf-8")
    # Limit extraction to the original review: other readers have similar code.
    old_review = block(text, "def _review_unexpected_overload(case, attempt, output_root):\n",
                       "def _collection_report(output_root, cases, outcomes, complete):\n")
    crc = block(old_review, '        p = case["parameters"]\n        if verified is True:\n',
                '        if any(native.get(k) != native.get("total", {}).get(k)')
    config = block(old_review, '        config = _read(root / "stream-config.json")\n',
                   '        hashes = {str(log): hashlib.sha256(log.read_bytes()).hexdigest()}\n')
    review = replace_once(replace_once(REVIEW, "@@CRC@@", crc), "@@CONFIG@@", config)
    result = replace_once(text, "def _collection_report(output_root, cases, outcomes, complete):\n",
                          review + "def _collection_report(output_root, cases, outcomes, complete):\n")
    for indent, variable in (("            ", "previous"), ("        ", "attempt")):
        result = replace_once(result, f"{indent}_review_unexpected_overload(case, {variable}, output_root)\n",
                              f"{indent}_review_collection_failure(case, {variable}, output_root)\n")
    old_count = '              "unexpected_overload_count": sum(v["status"] == "failed" and v.get("collection_review", {}).get("allowed") is True for v in values),\n'
    counts = "".join(f'              "{key}": sum(v["status"] == "failed" and v.get("collection_review", {{}}).get("allowed") is True and v["collection_review"].get("classification") == "{classification}" for v in values),\n'
                     for key, classification in (("unexpected_overload_count", "unexpected_controlled_overload"),
                                                 ("unstarted_measurement_count", "measurement_not_started")))
    result = replace_once(result, old_count, counts)
    result = replace_once(result, '    report = {"policy": "collect-unexpected-overload-v1",',
                          '    report = {"policy": "collect-audited-failures-v2",')
    result = replace_once(result, 'help="baseline only: retain audited unexpected overloads as failed and continue collection"',
                          'help="baseline only: collect audited unexpected overloads or unstarted measurements; both remain failed"')
    result = replace_once(result, "#!/usr/bin/env python3\n", "#!/usr/bin/env python3\n"
                          + f"# Collection v2; frozen v1 SHA256: {EXPECTED_SHA256}\n")
    if source.read_bytes() != raw:
        raise ValueError("frozen runner changed during preparation")
    with output.open("x", encoding="utf-8") as stream:
        stream.write(result)
    print("".join(difflib.unified_diff(text.splitlines(True), result.splitlines(True),
                                      fromfile=str(source), tofile=str(output))))
    print(f"COLLECTION_V2 {output}; generated={hashlib.sha256(result.encode()).hexdigest()}; source-only, UNTESTED")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(f"prepare_collection_v2: {error}")
