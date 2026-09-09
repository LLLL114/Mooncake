#!/usr/bin/env python3
"""Experiment A: bounded, fixed-arrival CPU simulations of real selector code."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT, REPO_ROOT
import argparse
import csv
import datetime
import hashlib
import itertools
import json
import math
import os
import pathlib
import random
import statistics
import subprocess

ROOT=pathlib.Path(__file__).resolve().parents[1]
REPO=REPO_ROOT
BASE={"rails":2,"request_bytes":1048576,"requests":20000,"warmup_requests":4000,
      "slice_bytes":65536,"production_split":True,"qos":True,"smart":True,
      "alpha":.01,"jitter":1e-9,"seed":1,"observe":False,"trace_every":0,
      "trace_limit":0,"window_ns":1000000,"poll_interval_ns":1000,
      "base_latency_ns":1000,"qp_depth":128,"deadline_ns":1000000000,
      "capacity_bytes_per_second":[12.5e9,12.5e9],"topology":"equal","memory_numa":0}


def quantile(values,p):
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)]


def cv(values):
    mean=statistics.mean(values)
    return statistics.pstdev(values)/mean if mean else None


def summarize(data):
    c=data["config"]; gap=c["arrival_interval_ns"]; start=c["warmup_requests"]*gap; stop=c["requests"]*gap
    rows=[w for w in data["windows"] if w["start_ns"]>=start and w["start_ns"]+c["window_ns"]<=stop]
    assert rows and data["allocation_bytes"]==data["completion_bytes"]
    duration=len(rows)*c["window_ns"]/1e9
    bw=[sum(r["done"][i] for r in rows)/duration for i in range(2)]
    lat=data["request_latency_ns"][c["warmup_requests"]:]
    cohorts=[quantile(lat[i:i+200],.99) for i in range(0,len(lat)-199,200)]
    # Reduce inevitable large-request burst aliasing; same bin width for each paired policy.
    group=max(1,math.ceil(20*gap/c["window_ns"]))
    coarse=[]
    for i in range(0,len(rows)-group+1,group):
        sums=[sum(r["done"][rail] for r in rows[i:i+group]) for rail in range(2)]
        assigned=[sum(r["assigned"][rail] for r in rows[i:i+group]) for rail in range(2)]
        coarse.append((sums,assigned))
    assert len(coarse)>=20
    shares=[a[0]/sum(a) for _,a in coarse if sum(a)]
    mean_u=[b/12.5e9 for b in bw]
    ps=[r["reference_p"][0] for r in rows]
    queue=[sum(r["queue_bytes"]) for r in rows]
    quarter=max(1,len(queue)//4)
    queue_growth=statistics.mean(queue[-quarter:])-statistics.mean(queue[:quarter])
    return {"topology":c["topology"],"size":c["request_bytes"],"load":c["offered_fraction"],
        "strategy":"smart" if c["smart"] else "rr","seed":c["seed"],
        "slice_count":data["split_count"],"slice_bytes":data["split_bytes"],"aggregate":data["aggregate_path"],
        "goodput_gbps":sum(bw)*8/1e9,"rail0_gbps":bw[0]*8/1e9,"rail1_gbps":bw[1]*8/1e9,
        "utilization":sum(bw)/25e9,"imbalance":abs(bw[0]-bw[1])/sum(bw),
        "jain":sum(mean_u)**2/(2*sum(u*u for u in mean_u)),
        "p99_us":quantile(lat,.99)/1000,"p50_us":quantile(lat,.5)/1000,
        "p99_cohort_cv":cv(cohorts),"p99_cohort_first_us":cohorts[0]/1000,"p99_cohort_last_us":cohorts[-1]/1000,
        "latency_deadline_fraction":sum(x>c["deadline_ns"] for x in lat)/len(lat),
        "throughput_cv":cv([sum(done) for done,_ in coarse]),"flow_bin_ms":group*c["window_ns"]/1e6,
        "flow_share_std":statistics.pstdev(shares),
        "reference_weight_std":statistics.pstdev(ps) if c["smart"] else None,
        "reference_weight_span":max(ps)-min(ps) if c["smart"] else None,
        "reference_weight_tv_per_second":sum(abs(a-b) for a,b in zip(ps,ps[1:]))/duration if c["smart"] else None,
        "decision_weight_step":data["decision_tv"]/max(1,data["decision_count"]-1) if c["smart"] else None,
        "best_switches_per_10k_decisions":data["best_changes"]*10000/max(1,data["decision_count"]) if c["smart"] else None,
        "first_slice_switches_per_10k_requests":data["first_slice_rail_changes"]*10000/len(lat),
        "model_queue_mean_bytes":statistics.mean(queue),"model_queue_growth_bytes":queue_growth,
        "model_queue_first_bytes":statistics.mean(queue[:quarter]),"model_queue_last_bytes":statistics.mean(queue[-quarter:]),
        "cohort_count":len(cohorts),"flow_bins":len(coarse),"digest":data["digest"],
        "measurement_seconds":(stop-start)/1e9,"resolved":data["requests"],"retry_migrations":0}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--validate-only",action="store_true")
    parser.add_argument("--sanitize",action="store_true")
    args=parser.parse_args()
    out=OUTPUT_ROOT/"runs/experiment-a"/("sanitize" if args.sanitize else "run")
    out.mkdir(parents=True,exist_ok=True)
    binary=OUTPUT_ROOT/"build"/("sanitize" if args.sanitize else "release")/"observed"
    checks=[]; runs=[]
    cpu=next(c for c in sorted(os.sched_getaffinity(0)) if c>=8)
    def execute(name,cfg,which=binary):
        path=out/name
        path.with_suffix(".config.json").write_text(json.dumps(cfg,indent=2)+"\n")
        cmd=["taskset","-c",str(cpu),str(which),str(path.with_suffix(".config.json")),str(path.with_suffix(".json"))]
        env=dict(os.environ)
        if args.sanitize: env.update(ASAN_OPTIONS="detect_leaks=1:abort_on_error=1",UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1")
        p=subprocess.run(cmd,capture_output=True,text=True,timeout=90,env=env)
        path.with_suffix(".stderr").write_text(p.stderr)
        if p.returncode: raise RuntimeError(f"{name}: {p.returncode}: {p.stderr}")
        data=json.loads(path.with_suffix(".json").read_text())
        assert data["allocation_bytes"]==data["completion_bytes"]
        assert all(row["inflight"]==0 for row in data["final_stats"])
        assert len(data["request_latency_ns"])==cfg["requests"]
        return data
    def check(name,value):
        checks.append({"name":name,"passed":bool(value)})
        assert value,name
        print("CHECK",name,"PASS",flush=True)
    for size,count,block,agg in [(65536,1,65536,False),(524288,8,65536,False),(1048576,16,65536,True),(16777216,32,524288,True)]:
        cfg={**BASE,"requests":500,"warmup_requests":100,"request_bytes":size,"arrival_interval_ns":max(1,round(size/22.5e9*1e9))}
        d=execute("validate_split_"+str(size),cfg)
        check("split_"+str(size),(d["split_count"],d["split_bytes"],d["aggregate_path"])==(count,block,agg))
    cfg={**BASE,"requests":1000,"warmup_requests":200,"arrival_interval_ns":50000}
    plain=execute("validate_clock",cfg,binary.parent/"clock")
    one=execute("validate_observed",cfg)
    fine=execute("validate_fine",{**cfg,"window_ns":10000})
    check("online_observation_no_policy_effect",plain["digest"]==one["digest"]==fine["digest"])
    qos_off=execute("validate_qos_off",{**cfg,"qos":False})
    check("high_priority_qos_equivalence",one["digest"]==qos_off["digest"])
    # The same rank0 memory location and bandwidth model used in P1 must match at 1MiB.
    legacy=execute("validate_legacy",{**cfg,"production_split":False})
    check("one_mib_p01_parity",one["digest"]==legacy["digest"])
    dual=execute("validate_dual_rr",{**cfg,"topology":"dual_numa","smart":False})
    check("rr_first_tier_only",dual["allocation_bytes"][1]==0)
    w=dual["windows"]
    check("rr_model_queue_visible",max(sum(r["queue_bytes"]) for r in w)>0 and all(sum(r["inflight"])==0 for r in w))
    check("full_window_byte_conservation",sum(sum(r["assigned"]) for r in one["windows"])==sum(one["allocation_bytes"]))
    (out/"validation.json").write_text(json.dumps(checks,indent=2)+"\n")
    if args.validate_only or args.sanitize:
        print("VALIDATION_DONE",len(checks),flush=True); return
    matrix=list(itertools.product(["equal","dual_numa"],[65536,524288,1048576,16777216],[.2,.6,.9,1.1],[1,2,19,12345,4294967295]))
    random.Random(908).shuffle(matrix)
    rng=random.Random(9008)
    for topology,size,load,seed in matrix:
        order=[True,False];rng.shuffle(order)
        for smart in order:
            name=f"{topology}_{size}_{int(load*100)}_{seed}_{'smart' if smart else 'rr'}"
            cfg={**BASE,"topology":topology,"request_bytes":size,"offered_fraction":load,
                 "seed":seed,"smart":smart,"arrival_interval_ns":round(size/(25e9*load)*1e9)}
            data=execute(name,cfg)
            row=summarize(data);row["run"]=name;runs.append(row)
            if len(runs)%16==0: print("MATRIX_PROGRESS",len(runs),"/320",flush=True)
    with (out/"metrics.csv").open("w",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=list(runs[0]));writer.writeheader();writer.writerows(runs)
    (out/"metrics.json").write_text(json.dumps(runs,indent=2)+"\n")
    examples=[]
    for load in [.2,.6,.9]:
        for smart in [True,False]:
            name=f"equal_1048576_{int(load*100)}_1_{'smart' if smart else 'rr'}"
            cfg=json.loads((out/(name+".config.json")).read_text());cfg["window_ns"]=10000
            cfg["window_output_from_ns"]=cfg["warmup_requests"]*cfg["arrival_interval_ns"]
            cfg["window_output_to_ns"]=cfg["window_output_from_ns"]+20000000
            data=execute("fine_"+name,cfg)
            original=next(r for r in runs if r["run"]==name)
            check("fine_parity_"+name,data["digest"]==original["digest"])
            start=cfg["warmup_requests"]*cfg["arrival_interval_ns"]
            excerpt=[r for r in data["windows"] if start<=r["start_ns"]<start+20000000]
            examples.append({"name":name,"config":cfg,"windows":excerpt})
    (out/"fine_examples.json").write_text(json.dumps(examples)+"\n")
    (out/"validation.json").write_text(json.dumps(checks,indent=2)+"\n")
    manifest={"utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),"cpu":cpu,
              "git_head":subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPO,text=True).strip(),
              "build":json.loads((binary.parent/"manifest.json").read_text()),
              "binary_sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),"runs":len(runs),
              "base_config":BASE,"seeds":[1,2,19,12345,4294967295],
              "nature":"CPU simulation, fixed-rate host WRITE arrivals, no packet/GPU/provider activity"}
    files=[ROOT/"simulator/driver.cpp",ROOT/"simulator/hooks.h",ROOT/"scripts/build.py",pathlib.Path(__file__)]
    manifest["experiment_source_sha256"]={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    (out/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print("EXPERIMENT_A_DONE",len(runs),len(checks),flush=True)


if __name__=="__main__":
    main()
