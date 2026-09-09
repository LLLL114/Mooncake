#!/usr/bin/env python3
"""Sampling/update cadence and CQ experiments; no production scheduler changes."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import argparse
import hashlib
import json
import math
import os
import pathlib
import random
import statistics
import subprocess
import sys

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from run_experiment_a import BASE, summarize
INTERVALS=[0,100000,1000000,5000000,10000000]
TAUS=[20000000,50000000,100000000]
LOADS=[.2,.6,.9]
SEEDS=[1,2,19,12345,4294967295]


def main():
    p=argparse.ArgumentParser();p.add_argument("--sanitize",action="store_true");p.add_argument("--validate-only",action="store_true")
    args=p.parse_args()
    out=OUTPUT_ROOT/"runs/experiment-c"/("sanitize" if args.sanitize else "run");out.mkdir(parents=True,exist_ok=True)
    build=OUTPUT_ROOT/"build"/("sanitize" if args.sanitize else "release")
    binary=build/"controlled";cases=[];checks=[]
    cpu=next(c for c in sorted(os.sched_getaffinity(0)) if c>=8)
    def execute(name,cfg,program=binary):
        inp=out/(name+".config.json");dest=out/(name+".json")
        if inp.exists() or dest.exists():raise FileExistsError("Archive existing run before rerunning: "+str(inp))
        inp.write_text(json.dumps(cfg,indent=2)+"\n")
        env=dict(os.environ)
        if args.sanitize:env.update(ASAN_OPTIONS="detect_leaks=1:abort_on_error=1",UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1")
        r=subprocess.run(["taskset","-c",str(cpu),str(program),str(inp),str(dest)],capture_output=True,text=True,timeout=120,env=env)
        (out/(name+".stderr")).write_text(r.stderr)
        if r.returncode:raise RuntimeError(name+": "+r.stderr)
        raw=dest.read_bytes();d=json.loads(raw)
        cases.append({"name":name,"program":program.name,"input_sha256":hashlib.sha256(inp.read_bytes()).hexdigest(),"result_sha256":hashlib.sha256(raw).hexdigest()})
        if not cfg.get("temporal_estimator"):
            assert d["allocation_bytes"]==d["completion_bytes"] and all(r["inflight"]==0 for r in d["final_stats"])
        return d
    def check(name,condition):
        checks.append({"name":name,"passed":bool(condition)});assert condition,name;print("CHECK",name,"PASS",flush=True)
    def scalar(name,period=10000,interval=0,tau=0,alpha=.01,**extra):
        return execute(name,{"temporal_estimator":True,"alpha":alpha,"sample_period_ns":period,
                            "sample_duration_ns":400000000,"update_interval_ns":interval,"time_constant_ns":tau,**extra})
    cfg={**BASE,"requests":1000,"warmup_requests":200,"arrival_interval_ns":50000}
    raw=execute("validate_original",cfg,build/"observed")
    direct=execute("validate_no_gate",cfg)
    check("zero_gate_preserves_original",raw["digest"]==direct["digest"] and raw["request_latency_ns"]==direct["request_latency_ns"])
    sparse=execute("validate_all_skipped",{**cfg,"update_interval_ns":1000000000})
    check("skipped_updates_still_release_quota",all(m["accepted"]==0 for m in sparse["sample_meters"]) and all(r["inflight"]==0 for r in sparse["final_stats"]))
    same=execute("validate_observation_only",{**cfg,"window_ns":10000000})
    check("observation_window_not_control",same["digest"]==direct["digest"])
    cqcfg={**cfg,"poll_interval_ns":100000}
    native_head=execute("validate_cq_head_native",cqcfg,build/"observed")
    native_tail=execute("validate_cq_tail_native",{**cqcfg,"sample_position":"tail"},build/"observed")
    check("cq_tail_bookkeeping_noninvasive",native_head["digest"]==native_tail["digest"])
    replay_head=scalar("validate_replay_head",period=100000,interval=1000000,batch_values_GBps=[2,4,6,8])
    replay_tail=scalar("validate_replay_tail",period=100000,interval=1000000,batch_values_GBps=[2,4,6,8],sample_position="tail")
    check("same_cadence_different_representative",abs(replay_head["final_GBps"]-2)<1e-8 and abs(replay_tail["final_GBps"]-8)<1e-8 and replay_head["sample_meters"][0]["accepted"]==replay_tail["sample_meters"][0]["accepted"])
    irregular=scalar("validate_actual_dt",tau=5000000,sample_times_ns=[100000,200000,8000000,11000000])
    check("tau_uses_actual_elapsed",abs(irregular["final_GBps"]-(5+5*math.exp(-11/5)))<1e-9)
    for interval in [100000,1000000,5000000,10000000]:
        fixed=scalar("validate_fixed_"+str(interval),interval=interval)
        timed=scalar("validate_tau_"+str(interval),interval=interval,tau=50000000)
        check("fixed_first_update_"+str(interval),fixed["t95_ns"]==interval)
        theory=math.ceil(-math.log(.05)*50000000/interval)*interval
        check("time_constant_step_"+str(interval),timed["t95_ns"]==theory)
        check("minimum_update_interval_"+str(interval),timed["sample_meters"][0]["min_interval_ns"]>=interval)
    def save():
        (out/"validation.json").write_text(json.dumps(checks,indent=2)+"\n")
        (out/"case-manifest.json").write_text(json.dumps(cases,indent=2)+"\n")
    save()
    if args.validate_only or args.sanitize:print("C_VALIDATION_DONE",len(checks),flush=True);return
    temporal=[]
    for period in [10000,100000,1000000]:
        for tau in [0,50000000]:
            name=f"rate_p{period}_t{tau}"
            d=scalar(name,period=period,tau=tau,alpha=.9)
            expected=math.ceil(math.log(.05)/math.log(.9))*period if not tau else math.ceil(-math.log(.05)*tau/period)*period
            check("event_rate_step_"+name,d["t95_ns"]==expected)
            temporal.append({"name":name,**d})
    for interval in INTERVALS:
        for tau in [0]+TAUS:
            d=scalar(f"cadence_g{interval}_t{tau}",interval=interval,tau=tau)
            temporal.append({"name":f"cadence_g{interval}_t{tau}",**d})
    rows=[];cache={}
    def config(interval,tau,poll,load,seed,position):
        gap=round(1048576/(25e9*load)*1e9);warm=math.ceil(1000000000/gap)
        return {**BASE,"alpha":.01,"offered_fraction":load,"seed":seed,"requests":warm*2,"warmup_requests":warm,
                "arrival_interval_ns":gap,"poll_interval_ns":poll,"update_interval_ns":interval,"time_constant_ns":tau,
                "sample_position":position,"request_trace_from":warm,"request_trace_to":warm+100}
    def run(interval,tau,poll,load,seed,position="head",phase="main"):
        key=(interval,tau,poll,load,seed,position)
        if key in cache:return cache[key]
        name=f"g{interval}_t{tau}_p{poll}_l{int(load*100)}_s{seed}_{position}"
        cfg=config(*key);d=execute(name,cfg);s=summarize(d)
        meters=d["sample_meters"];duration=(cfg["requests"]-cfg["warmup_requests"])*cfg["arrival_interval_ns"]/1e9
        seen=sum(m["seen"] for m in meters);accepted=sum(m["accepted"] for m in meters)
        raw_mean=sum(m["raw_sample_mean_GBps"]*m["seen"] for m in meters)/seen
        chosen_mean=sum(m["accepted_sample_mean_GBps"]*m["accepted"] for m in meters)/accepted
        s.update(name=name,phase=phase,update_interval_ns=interval,time_constant_ns=tau,poll_interval_ns=poll,
                 sample_position=position,allocation_step=d["allocation_step"],allocation_std=d["allocation_rail0_std"],
                 balanced_fraction=d["allocation_histogram"][8]/d["allocation_samples"],
                 accepted_updates_per_rail_second=accepted/2/duration,
                 mean_actual_update_us=sum(m["mean_interval_ns"]*m["accepted"] for m in meters)/accepted/1000,
                 raw_sample_mean_GBps=raw_mean,chosen_sample_mean_GBps=chosen_mean,selection_bias_ratio=chosen_mean/raw_mean,
                 mean_actual_alpha=sum(m["mean_alpha"]*m["accepted"] for m in meters)/accepted,
                 cq_batch_mean=seen/sum(m["seen"]-m["same_timestamp_samples"] for m in meters),
                 request_decisions=d["request_decisions"],sample_meters=meters)
        assert all(m["min_interval_ns"]>=interval for m in meters)
        if not interval and not tau and position=="head" and poll==1000:
            old=json.loads((OUTPUT_ROOT/f"runs/experiment-a/run/equal_1048576_{int(load*100)}_{seed}_smart.json").read_text())
            n=min(len(old["request_latency_ns"]),len(d["request_latency_ns"]))
            assert d["request_latency_ns"][:n]==old["request_latency_ns"][:n]
        rows.append(s);cache[key]=s
        if len(rows)%15==0:print("C_MATRIX_PROGRESS",len(rows),"/525",flush=True)
        return s
    matrix=[(g,t,1000,l,seed) for g in INTERVALS for t in [0]+TAUS for l in LOADS for seed in SEEDS]
    random.Random(9083).shuffle(matrix)
    for parameters in matrix:run(*parameters)
    for poll in [10000,50000,100000]:
        for interval,tau in [(0,0),(1000000,0),(1000000,50000000)]:
            for load in LOADS:
                for seed in SEEDS:run(interval,tau,poll,load,seed,phase="cq")
        for tau in [0,50000000]:
            for load in LOADS:
                for seed in SEEDS:run(1000000,tau,poll,load,seed,"tail",phase="tail")
    observations=[]
    for interval,tau in [(0,0),(1000000,0),(1000000,50000000)]:
        for window in [10000,100000,1000000,10000000]:
            cfg=config(interval,tau,1000,.9,1,"head");cfg["window_ns"]=window
            cfg["window_output_from_ns"]=1010000000;cfg["window_output_to_ns"]=1990000000
            name=f"observation_g{interval}_t{tau}_w{window}"
            d=execute(name,cfg);s=summarize(d)
            common=[w for w in d["windows"] if 1010000000<=w["start_ns"] and w["start_ns"]+window<=1990000000]
            ps=[w["reference_p"][0] for w in common]
            check("same_policy_different_observation_"+name,d["digest"]==cache[(interval,tau,1000,.9,1,"head")]["digest"])
            observations.append({"name":name,"update_interval_ns":interval,"time_constant_ns":tau,"observation_ns":window,
                                 "digest":d["digest"],"reference_weight_std":statistics.pstdev(ps),
                                 "reference_weight_tv_per_second":sum(abs(a-b) for a,b in zip(ps,ps[1:]))/.98,"p99_us":s["p99_us"]})
    # Representative stream has no feedback: only selection position differs.
    representative={"head":replay_head,"tail":replay_tail}
    check("unique_case_artifacts",len({c["name"] for c in cases})==len(cases))
    result={"rows":rows,"temporal":temporal,"observations":observations,"representative":representative,
            "checks":checks,"case_count":len(cases),"matrix_count":len(rows),"cpu":cpu,
            "config_axes":{"intervals_ns":INTERVALS,"taus_ns":TAUS,"loads":LOADS,"seeds":SEEDS},
            "gate_semantics":"first eligible successful sample per NIC after minimum interval; tail waits for last completion in same device/timestamp group; no fixed wall-clock timer",
            "binary_sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),
            "build":json.loads((build/"manifest.json").read_text())}
    files=[ROOT/"simulator/driver.cpp",ROOT/"simulator/hooks.h",ROOT/"simulator/sampling.h",ROOT/"scripts/build.py",ROOT/"scripts/run_experiment_a.py",pathlib.Path(__file__)]
    result["source_sha256"]={str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    (out/"summary.json").write_text(json.dumps(result,indent=2)+"\n");save()
    print("EXPERIMENT_C_DONE",len(rows),len(cases),len(checks),flush=True)


if __name__=="__main__":main()
