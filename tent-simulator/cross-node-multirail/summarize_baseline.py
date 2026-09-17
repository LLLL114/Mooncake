#!/usr/bin/env python3
"""Server-only, sequential A-baseline report; stdlib plus analyze_observer."""
import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
import statistics

from analyze_observer import analyze, aligned, losses, nic_reference, spread

DONE = {"success", "completed_overload", "completed_overload_unverified"}
STATUSES = (*sorted(DONE), "failed", "running")
CORE = ("goodput_gbps", "p99_ms", "throughput_250ms_sd_gbps", "throughput_250ms_cv",
        "p99_window_sd_ms", "p99_window_cv", "p99_valid_windows", "p99_window_ms",
        "allocation_250ms_tv_sum", "allocation_250ms_tv_per_second",
        "ewma_lower_hit_fraction", "retry_endpoint", "retry_post", "retry_cq", "retry_events",
        "capacity_d0_gbps", "capacity_mode_gbps", "goodput_over_d0_reference")
WEIGHTS = tuple(f"{kind}_{metric}" for kind in ("consumed", "single", "probe", "other_selection")
                for metric in ("comparisons", "tv_sum", "tv_per_second",
                               "changes_0.001_per_second", "changes_0.01_per_second", "changes_0.05_per_second"))
ERRORS = (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError)
NOTES = """仅报告 A 原算法基线；不宣称 E/F 或任何新算法达标。API 每 batch 1 请求，Q 是在途窗口。
实验固定 CPU/内存 NUMA0；实际 affinity/NUMA 请求及 receiver 环境保存在各 run 的 evidence。
单远端 NIC 是共享瓶颈限制；同 size/Q 且库指纹匹配的 capacity.json 中 D0 是实测参考，绝不用 C0+C1 充当共享总容量，也不宣称硬件峰值。
吞吐按测量期成功完成字节计 Gbps；整体 P99 为成功请求的 planned-arrival 端到端时延（含 drain），过载/失败/未验证数据仅作诊断。
SD 为总体标准差，CV=SD/均值（零均值为 None）；250ms 吞吐仅用完整且对齐的窗口。P99 窗至少 500 成功样本且至少 20 有效完整窗；16MiB 保留冻结策略，不评估窗 P99 波动。
consumed 为真实 batch 权重，single/probe/other_selection 单列诊断；变化频率为同线程/上下文 TV > 0.001/0.01/0.05 的原生计数/测量秒数，不拼接跨上下文决策。
allocation 字节先将原生 50ms 桶求和到 250ms 再算 NIC 份额；空窗打断相邻 TV，尾部不完整窗排除。正常选路变化不称为迁移。
EWMA 下界命中为 new_equals_min/updates；retry 仅为 retry_endpoint/post/cq 原生事件数，不能推导唯一跨轨重发或迁移。
每 run 等权，分组为 mode/size/load/Q，再按状态、过载和证据质量隔离；展示中位数 [最小,最大]，不合并请求样本或补零。
None/CSV 空值表示缺失或不适用；缺失原因、所有 attempt 索引及失败证据见 summary.json / runs.csv。统计仅覆盖已观测数据，不外推 observer 丢失部分。
矩阵覆盖、最后 attempt 被套件接受及全部实测通过分别报告；失败后重试成功仍保留原生超时证据。正式测量没有接受任何请求的失败样本不计算吞吐、P99或选路波动，容量参考仍保留。
本脚本尚待主在 SSH 服务器验证；报告中的状态来自 state.json，不代表重新完成 payload 校验。"""


def read(path, issues):
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("expected object")
        return value
    except ERRORS as error:
        issues.append(f"{path}: {error}")
        return {}


def weight_metrics(entries, prefix, metrics):
    weights = [e["weight"] for e in entries if "comparisons" in e["weight"]]
    if not weights:
        return
    for key in ("comparisons", "tv_sum", "tv_per_second"):
        metrics[f"{prefix}_{key}"] = sum(w[key] for w in weights)
    for index, delta in enumerate((0.001, 0.01, 0.05)):
        metrics[f"{prefix}_changes_{delta}_per_second"] = sum(
            w["thresholds"][index]["events_per_second"] for w in weights)


def request_metrics(manifest, summary, windows, metrics, evidence):
    start, end = manifest["measurement_start_ns"], manifest["measurement_end_ns"]
    if end <= start or (summary.get("measurement_start_ns"), summary.get("measurement_end_ns")) != (start, end):
        raise ValueError("request summary/manifest interval mismatch")
    payload = summary["throughput"]["success_bytes"]
    if not math.isclose(summary["throughput"]["bits_per_second"], payload * 8e9 / (end - start), rel_tol=1e-9):
        raise ValueError("summary throughput disagrees with completion bytes/duration")
    metrics["goodput_gbps"] = payload * 8 / (end - start)
    p99 = summary["latency_success"]["end_to_end_ns"]["p99"]
    metrics["p99_ms"] = p99 / 1e6 if p99 is not None else None
    throughput = windows.get("throughput", {}).get("250ms", [])
    if aligned(throughput, start, end, 250_000_000):
        stats = spread([w["bits_per_second"] / 1e9 for w in throughput if w["duration_ns"] == 250_000_000])
        metrics.update(throughput_250ms_sd_gbps=stats.get("sd"), throughput_250ms_cv=stats.get("cv"))
    else:
        evidence["throughput_reason"] = "250ms windows missing or misaligned"
    latency = windows.get("latency", {})
    width, items = latency.get("window_ns"), latency.get("windows", [])
    if type(width) is not int or width <= 0 or not aligned(items, start, end, width):
        evidence["p99_reason"] = "arrival windows missing or misaligned"
        return
    values = [w["p99_end_to_end_ns"] / 1e6 for w in items if w["success_requests"] >= 500
              and w["end_ns"] - w["start_ns"] == width and w["p99_end_to_end_ns"] is not None]
    metrics.update(p99_valid_windows=len(values), p99_window_ms=width / 1e6)
    stats = spread(values, 20)
    if manifest.get("stream_config", {}).get("size") == 16 * 1024 * 1024:
        stats = {"reason": "frozen 16MiB baseline policy: global P99 only"}
    metrics.update(p99_window_sd_ms=stats.get("sd"), p99_window_cv=stats.get("cv"))
    evidence["p99_variability"] = stats


def observer_metrics(observer, manifest, capacity, metrics, evidence):
    evidence["observer_loss"] = losses(observer, "observer")
    evidence["observer_complete"] = observer.get("incomplete") is False and not evidence["observer_loss"]
    analysis = analyze(observer, manifest, capacity)
    native = analysis["SUMMARY"]
    evidence["analysis_loss"] = analysis["loss_evidence"]
    evidence["observer_reason"] = native.get("observer_metrics")
    evidence["queue_sampling"] = native.get("queue_sampling")
    if "clamp_totals" not in native:
        return
    consumed, diagnostics = native["consumed_weights"], native["selection_diagnostics"]
    evidence.update(consumed_weights=consumed, selection_diagnostics=diagnostics)
    weight_metrics(consumed if isinstance(consumed, list) else [], "consumed", metrics)
    for mode, name in ((0, "single"), (2, "probe"), (3, "other_selection")):
        weight_metrics([e for e in diagnostics if e["definition"]["mode"] == mode], name, metrics)
    clamp = native["clamp_totals"]
    evidence["clamp_totals"] = clamp
    if clamp.get("updates"):
        metrics["ewma_lower_hit_fraction"] = clamp["new_equals_min"] / clamp["updates"]
    for kind in ("retry_endpoint", "retry_post", "retry_cq"):
        metrics[kind] = native["io_diagnostics"][kind]["events"]
    metrics["retry_events"] = sum(metrics[k] for k in ("retry_endpoint", "retry_post", "retry_cq"))
    grid = analysis["windows"]["250ms"]
    allocation = grid["variability"]["allocation"]
    tv = allocation["adjacent_share_tv"]
    metrics.update(allocation_250ms_tv_sum=tv.get("sum"), allocation_250ms_tv_per_second=tv.get("per_second"))
    mapping = manifest.get("environment", {}).get("nic_id_to_name", {})
    active = [name.strip() for name in manifest.get("environment", {}).get("nic", "").split(",")]
    shares = allocation["share_by_dev"]
    if not mapping or any(list(mapping.values()).count(name) != 1 for name in active) or any(dev not in mapping or mapping[dev] not in active for dev in shares):
        evidence["allocation_share_reason"] = "allocation NicID missing from real manifest map/active NICs"
        return
    nonempty = any(w["shares"]["allocation"] is not None and w["end_ns"] - w["start_ns"] == 250_000_000
                   for w in grid["windows"])
    for name in active:
        ids = [dev for dev in mapping if mapping[dev] == name]
        metrics[f"allocation_250ms_share_sd_{name}"] = (
            shares.get(ids[0], {}).get("sd", 0.0 if nonempty else None) if len(ids) == 1 else None)


def run(path, capacity):
    issues, metrics, evidence = [], dict.fromkeys(CORE + WEIGHTS), {}
    row = {"state_path": str(path), "status": "failed", "metrics": metrics, "evidence": evidence, "issues": issues}
    try:
        state = read(path, issues)
        case, attempts = state["case"], state["attempts"]
        last = attempts[-1] if attempts else {"status": "running"}
        row.update({k: case[k] for k in ("mode", "size", "load", "q", "repeat")})
        row.update(case_hash=state["caseHash"], raw_status=last["status"],
                   status=last["status"] if last["status"] in STATUSES else "failed",
                   overload=case.get("offered_exceeds_mode_capacity") is True or last["status"].startswith("completed_overload"),
                   run_path=last.get("runPath"), attempt=last.get("attempt"), error=last.get("error"))
        evidence["attempts"] = [{k: a.get(k) for k in ("attempt", "status", "runPath", "logPath", "exitCode", "error", "collection_review")}
                                for a in attempts]
        for previous in evidence["attempts"]:
            if previous["status"] == "failed" and previous.get("runPath"):
                previous_issues = []
                native = read(Path(previous["runPath"]) / "stream-summary.json", previous_issues)
                previous["native_failure"] = {k: native.get(k) for k in
                    ("return_code", "stop_reason", "failure_reason", "start_missed", "total", "measurement")}
                previous["evidence_issues"] = previous_issues
        if case.get("phase") != "baseline":
            raise ValueError("non-baseline case in baseline directory")
        if not row["run_path"] or row["status"] == "running":
            issues.append("no final run artifacts: missing runPath or running snapshot")
            return row
        root = Path(row["run_path"])
        row["observer_missing"] = not (root / "observer.json").is_file()
        manifest = read(root / "manifest.json", issues)
        accounting = manifest if "pending" in manifest else read(root / "stream-summary.json", issues)
        for key in ("accepted", "success", "failure", "pending", "drained", "requests_output_complete",
                    "overflow", "input_stopped_early", "watchdog_required", "native_return_code", "data_verified"):
            row[key] = manifest.get(key, accounting.get(key))
        evidence.update(environment=manifest.get("environment"), receiver=manifest.get("receiver"),
                        native_total=accounting.get("total"), manifest_loss=losses(manifest, "manifest"))
        row["failure_kind"] = (last.get("collection_review", {}).get("classification")
                               or (accounting.get("stop_reason") if row["status"] == "failed" else None))
        for name in ("failure", "correctness", "overload-analysis"):
            if (root / f"{name}.json").exists():
                evidence[name] = read(root / f"{name}.json", issues)
        config = manifest.get("stream_config", {})
        if (config.get("size"), config.get("window")) != (row["size"], row["q"]):
            raise ValueError("manifest size/Q differs from state case")
        reference = nic_reference(manifest, capacity)
        evidence["nic_capacity_reference"] = reference
        if "active_nics" in reference:
            entry = capacity["sizes"][str(row["size"])]
            evidence["capacity"] = {k: entry.get(k) for k in ("q", "reference_bytes_per_second", "mode_capacity_bytes_per_second", "saturation_confirmed")}
            caps = entry.get("mode_capacity_bytes_per_second", {})
            if entry.get("reference_bytes_per_second") == caps.get("D0") and all(
                    type(caps.get(m)) in (int, float) and math.isfinite(caps[m]) and caps[m] > 0 for m in ("S0", "S1", "D0")):
                metrics.update(capacity_d0_gbps=caps["D0"] * 8e-9, capacity_mode_gbps=caps[row["mode"]] * 8e-9)
                row["overload"] = row["overload"] or config.get("rate_bytes_per_second", 0) > caps[row["mode"]]
        if row["status"] == "failed" and accounting.get("measurement", {}).get("accepted") == 0:
            row["measurement_available"] = False
            evidence["measurement_exclusion"] = accounting.get("stop_reason") or "failed before any measured request"
            observer = read(root / "observer.json", issues)
            evidence["observer_complete"] = observer.get("incomplete") is False and not losses(observer, "observer")
            row["observer_complete"] = evidence["observer_complete"]
            return row
        row["measurement_available"] = True
        summary, windows = read(root / "summary.json", issues), read(root / "windows.json", issues)
        row["excluded_non_submitted_terminal_records"] = summary.get("excluded_non_submitted_terminal_records")
        if summary and row["status"] != "completed_overload_unverified":
            try:
                request_metrics(manifest, summary, windows, metrics, evidence)
            except ERRORS as error:
                issues.append(f"request metrics: {error}")
        del summary, windows
        if metrics["goodput_gbps"] is not None and metrics["capacity_d0_gbps"]:
            metrics["goodput_over_d0_reference"] = metrics["goodput_gbps"] / metrics["capacity_d0_gbps"]
        try:
            observer_metrics(read(root / "observer.json", issues), manifest, capacity, metrics, evidence)
        except ERRORS as error:
            issues.append(f"observer metrics: {error}")
    except ERRORS as error:
        issues.append(str(error))
    row["observer_complete"] = evidence.get("observer_complete")
    return row


def interval(values):
    values = [v for v in values if type(v) in (int, float) and math.isfinite(v)]
    return {"n": len(values), "median": statistics.median(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=5, help="planned repeats; default 180 cases")
    args = parser.parse_args()
    source = Path(__file__).resolve()
    repo = next((p for p in source.parents if (p / ".git").exists()), source.parent)
    output, suite = args.output_root.expanduser().resolve(), args.suite_root.expanduser().resolve()
    if output == repo or repo in output.parents or args.repetitions < 1 or not suite.is_dir():
        parser.error("require outside-repository output, positive repetitions and existing suite directory")
    names = ("runs.csv", "summary.json", "BASELINE_REPORT.md")
    if any((output / name).exists() for name in names):
        parser.error("refusing to overwrite report files; choose a fresh output-root")
    capacity_issues = []
    capacity = read(suite / "capacity.json", capacity_issues)
    rows = [run(path, capacity) for path in sorted((suite / "baseline").glob("*/state.json"))]
    expected = {(m, s, l, capacity.get("sizes", {}).get(str(s), {}).get("q"), r)
                for m in ("S0", "S1", "D0") for s in (65536, 1048576, 16777216)
                for l in ("20pct", "60pct", "90pct", "saturated") for r in range(1, args.repetitions + 1)}
    identities = Counter(tuple(row.get(k) for k in ("mode", "size", "load", "q", "repeat")) for row in rows)
    missing = sorted(expected - identities.keys())
    matrix_covered = (not missing and set(identities) == expected and all(n == 1 for n in identities.values())
                      and all(r["raw_status"] in DONE | {"failed"} for r in rows))
    complete = matrix_covered and all(r["status"] in DONE for r in rows)
    buckets = defaultdict(list)
    for row in rows:
        clean = (not row["issues"] and row.get("data_verified") is True and row.get("pending") == 0
                 and row.get("drained") is True and row.get("requests_output_complete") is True
                 and row.get("failure") == 0 and row.get("native_return_code") == 0 and row.get("watchdog_required") is False
                 and row["evidence"].get("observer_complete") is True and not row["evidence"].get("manifest_loss")
                 and not any(row["evidence"].get(k) for k in ("observer_reason", "throughput_reason", "p99_reason")))
        row["quality"] = "verified_observed" if clean else "diagnostic_or_missing"
        key = tuple(row.get(k) for k in ("mode", "size", "load", "q", "status", "overload", "failure_kind", "quality"))
        buckets[key].append(row)
    groups = [{"key": dict(zip(("mode", "size", "load", "q", "status", "overload", "failure_kind", "quality"), key)), "runs": len(items),
               "metrics": {k: interval([r["metrics"].get(k) for r in items]) for k in sorted(set().union(*(r["metrics"] for r in items)))}}
              for key, items in sorted(buckets.items(), key=lambda item: str(item[0]))]
    report = {"schema_version": 1, "scope": "A baseline only", "suite_root": str(suite), "complete": complete,
              "matrix_covered": matrix_covered,
              "all_measurements_verified": matrix_covered and all(r["status"] == "success" and r["quality"] == "verified_observed" for r in rows),
              "expected_cases": len(expected), "observed_states": len(rows), "missing_cases": missing,
              "duplicate_or_unexpected": [{"case": key, "count": n} for key, n in identities.items() if n != 1 or key not in expected],
              "status_counts": {s: sum(r["status"] == s for r in rows) for s in STATUSES},
              "capacity_issues": capacity_issues, "notes": NOTES, "groups": groups, "runs": rows}
    lines = ["# A 基线报告", "", f"矩阵覆盖：{'已记录全部终态（不等于全部验证通过）' if matrix_covered else '未完成'}；预期 {len(expected)}，已发现 {len(rows)}，缺少 {len(missing)}。",
             f"全部测量通过验证：{report['all_measurements_verified']}；最后 attempt 均被原套件接受：{complete}。",
             json.dumps(report["status_counts"], ensure_ascii=False), "", NOTES, "",
             "容量读取问题：" + json.dumps(capacity_issues, ensure_ascii=False), "",
             "## 逐 run 核查", "", "| case / attempt | 状态 / 过载 | pending / failure | 数据验证 / observer 完整 / observer 缺失 |",
             "|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row.get('case_hash', row['state_path'])} / {row.get('attempt')} | {row['status']} / {row.get('overload')} | "
                     f"{row.get('pending')} / {row.get('failure')} | {row.get('data_verified')} / {row['evidence'].get('observer_complete')} / {row.get('observer_missing')} |")
    lines.append("")
    for group in groups:
        lines += ["## " + " / ".join(f"{k}={v}" for k, v in group["key"].items()), "",
                  f"运行数：{group['runs']}。", "", "| 指标 | 有效 run 数 | 中位数 [最小,最大] |", "|---|---:|---|"]
        for key, value in group["metrics"].items():
            result = "None" if not value["n"] else f"{value['median']:.6g} [{value['min']:.6g}, {value['max']:.6g}]"
            lines.append(f"| {key} | {value['n']} | {result} |")
        lines.append("")
    lines += ["## 失败、非 success 与缺失证据（含历史失败 attempt）", ""]
    for row in rows:
        failures = [a for a in row["evidence"].get("attempts", []) if a["status"] not in DONE]
        if row["status"] != "success" or row["issues"] or failures:
            lines.append("- " + json.dumps({"state": row["state_path"], "status": row["status"], "error": row.get("error"), "issues": row["issues"], "attempts": failures}, ensure_ascii=False))
    output.mkdir(parents=True, exist_ok=True)
    flattened = [{**{k: v for k, v in r.items() if k != "metrics"}, **r["metrics"]} for r in rows]
    fields = sorted(set().union(*(r.keys() for r in flattened))) if flattened else ["state_path", "status"]
    with (output / "runs.csv").open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: json.dumps(v, ensure_ascii=False, allow_nan=False) if isinstance(v, (dict, list)) else v for k, v in r.items()} for r in flattened)
    with (output / "summary.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    with (output / "BASELINE_REPORT.md").open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
