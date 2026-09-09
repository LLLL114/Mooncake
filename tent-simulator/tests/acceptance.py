#!/usr/bin/env python3
"""P1 correctness and paired observer-cost acceptance, no external Python deps."""

import sys as _sys
_sys.dont_write_bytecode = True
from pathlib import Path as _BootstrapPath
_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1] / "scripts"))
from experiment_paths import OUTPUT_ROOT
import argparse
import json
import math
import os
import pathlib
import random
import statistics
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]


import sys
sys.path.insert(0, str(ROOT / "scripts"))
from analyze import analyze, jain, percentile, total_variation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sanitize", action="store_true")
    parser.add_argument("--skip-overhead", action="store_true")
    parser.add_argument("--output-dir", type=pathlib.Path)
    args = parser.parse_args()
    build = OUTPUT_ROOT / "build" / ("sanitize" if args.sanitize else "release")
    out = args.output_dir or OUTPUT_ROOT / "runs/p01" / ("sanitize" if args.sanitize else "acceptance")
    out.mkdir(parents=True, exist_ok=True)
    base = json.loads((ROOT/"configs/p01-smoke.json").read_text())
    checks = []
    counter = 0
    # One idle-capable CPU, no root scheduling or affinity changes outside child.
    allowed = sorted(os.sched_getaffinity(0))
    cpu = next((c for c in allowed if c >= 8), allowed[0])

    def run(binary, config):
        nonlocal counter
        counter += 1
        path = out / f"{counter:04d}"
        path.with_suffix(".config.json").write_text(json.dumps(config, indent=2)+"\n")
        env = dict(os.environ)
        if args.sanitize:
            env["ASAN_OPTIONS"] = "detect_leaks=1:abort_on_error=1"
            env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
        cmd = ["taskset", "-c", str(cpu), str(build/binary), str(path.with_suffix(".config.json")), str(path.with_suffix(".json"))]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env)
        path.with_suffix(".stderr").write_text(p.stderr)
        if p.returncode:
            raise RuntimeError(f"{cmd}: {p.returncode}: {p.stderr}")
        return json.loads(path.with_suffix(".json").read_text())

    def check(name, condition, details=None):
        checks.append({"name": name, "passed": bool(condition), "details": details})
        print(name, "PASS" if condition else "FAIL", flush=True)
        if not condition:
            (out/"failure.json").write_text(json.dumps(checks,indent=2)+"\n")
            raise AssertionError(name)

    for name in ["ewma", "clamp", "failure_release", "mask", "unavailable", "tail_charge",
                 "reroute", "rounding", "probe", "round_robin"]:
        result = run("observed", {"case": name, "seed": 1})
        check(name, result["passed"], result)

    check("metric_known_vectors", percentile([1,2,3,4,100],.99)==100
          and total_variation([[.5,.5],[.75,.25],[.5,.5]],2)==.25
          and jain([1,1])==1 and jain([0,0]) is None)
    full = run("observed", base)
    repeat = run("observed", base)
    check("deterministic_full_trace", full["trace"]==repeat["trace"] and full["digest"]==repeat["digest"])
    check("bounded_complete", full["slices"]==16000 and full["deadline_exceeded"]==0
          and full["allocation_bytes"]==full["completion_bytes"] and full["trace_dropped"]==0)
    candidates = [r for r in full["trace"] if r["event"]=="candidates"]
    metrics = analyze(full, 1000000)
    check("window_byte_conservation", sum(sum(r["completed_bytes"]) for r in metrics["rows"])==sum(full["completion_bytes"]))
    check("reference_weights", all(abs(sum(w["p"] for w in r["reference_weights"])-1)<1e-12 and len(r["stats_before"])==2 for r in candidates))
    check("candidate_weights", len(candidates)==base["requests"]
          and all(abs(sum(w["p"] for w in r["weights"])-1)<1e-12 for r in candidates))
    post = {(r["request"],r["slice"]):r["time_ns"] for r in full["trace"] if r["event"]=="post"}
    done = [r for r in full["trace"] if r["event"]=="complete"]
    check("event_causality", len(post)==len(done)==16000
          and all(post[(r["request"],r["slice"])]==r["post_ns"] < r["time_ns"] for r in done))
    for seed in [1,2,19,12345,4294967295]:
        cfg = {**base,"requests":1500,"observe":False,"seed":seed}
        native = run("native",cfg)
        virtual = run("clock",cfg)
        traced = run("observed",{**cfg,"observe":True,"trace_every":17})
        check(f"native_clock_observed_parity_seed_{seed}",
              native["digest"]==virtual["digest"]==traced["digest"]
              and native["request_latency_ns"]==traced["request_latency_ns"])
    qos = {**base,"qos":True,"requests":1500}
    a,b = run("clock",qos),run("observed",qos)
    check("virtual_clock_qos_parity", a["digest"]==b["digest"])
    small = run("observed",{**base,"requests":30,"trace_limit":10})
    rejected = False
    try:
        analyze(small, 1000000)
    except ValueError:
        rejected = True
    check("reject_incomplete_metrics", rejected)
    check("trace_overflow_visible", small["trace_dropped"]>0 and len(small["trace"])==10)
    isolated = {**base,"rails":1,"capacity_bytes_per_second":[12.5e9],"requests":20,
                "request_bytes":65536,"poll_interval_ns":1,"arrival_interval_ns":100000}
    analytic = run("observed",isolated)
    expected = math.ceil(65536/12.5e9*1e9)+1000
    check("single_rail_analytic_latency", analytic["p50_ns"]==analytic["p99_ns"]==expected)
    rr = run("observed",{**base,"smart":False})
    check("rr_exact_byte_balance", rr["allocation_bytes"][0]==rr["allocation_bytes"][1])
    shallow = run("observed",{**base,"requests":300,"qp_depth":1})
    check("qp_credit_bound", max(r["qp_posted"] for r in shallow["trace"] if r["event"]=="post")==1
          and all(r["inflight"]==0 for r in shallow["final_stats"]))
    tail = run("observed",{**base,"requests":20,"request_bytes":100001})
    check("tail_physical_byte_conservation",sum(tail["completion_bytes"])==2000020
          and all(r["inflight"]==0 for r in tail["final_stats"]))
    slow = run("observed",{**isolated,"arrival_interval_ns":1,"deadline_ns":1})
    check("deadline_exceedance_visible",slow["deadline_exceeded"]==20)

    overhead = None
    if not args.skip_overhead:
        cfg = {**base,"requests":300000,"observe":False,"trace_every":16384}
        run("clock",cfg)
        run("observed",{**cfg,"observe":True})
        pairs = []
        rng = random.Random(814)
        for i in range(9):
            order = ["clock","observed"]; rng.shuffle(order)
            rows = {}
            for binary in order:
                rows[binary] = run(binary,{**cfg,"observe":binary=="observed"})
            require_same = rows["clock"]["digest"]==rows["observed"]["digest"]
            check(f"overhead_pair_{i}_parity",require_same)
            pairs.append({"order":order,"off_seconds":rows["clock"]["wall_seconds"],
                "on_seconds":rows["observed"]["wall_seconds"],
                "relative":rows["observed"]["wall_seconds"]/rows["clock"]["wall_seconds"]-1})
        deltas = [p["relative"] for p in pairs]
        bootstrap = sorted(statistics.median(rng.choices(deltas,k=len(deltas))) for _ in range(5000))
        overhead = {"pairs":pairs,"median_relative":statistics.median(deltas),
                    "median_ci95":[bootstrap[124],bootstrap[4874]],"target_relative":.01,
                    "target_met":statistics.median(deltas)<=.01,
                    "scope":"single-CPU simulator core wall time, excludes final JSON I/O; not RDMA throughput/P99"}
        print("OVERHEAD",json.dumps(overhead),flush=True)
    summary = {"checks":checks,"check_count":len(checks),"cpu":cpu,
               "all_correctness_passed":all(c["passed"] for c in checks),
               "overhead":overhead,"runs":counter,"utc_epoch":time.time(),
               "smoke":{k:full[k] for k in ["digest","requests","slices","p99_ns","deadline_exceeded","trace_dropped"]}}
    (out/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print("ACCEPTANCE_DONE",out/"summary.json",flush=True)


if __name__=="__main__":
    main()
