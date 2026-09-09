#!/usr/bin/env python3
"""Only alpha changes in the selector; service changes belong to the environment."""

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
ALPHAS=[.01,.5,.9,.99,.999]
SEEDS=[1,2,19,12345,4294967295]
LOADS=[.2,.6,.9]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--validate-only",action="store_true")
    parser.add_argument("--sanitize",action="store_true")
    args=parser.parse_args()
    folder="sanitize" if args.sanitize else "run"
    out=OUTPUT_ROOT/"runs/experiment-b"/folder;out.mkdir(parents=True,exist_ok=True)
    binary=OUTPUT_ROOT/"build"/("sanitize" if args.sanitize else "release")/"observed"
    cpu=next(c for c in sorted(os.sched_getaffinity(0)) if c>=8)
    checks=[]; run_count=0; case_manifest=[]
    def execute(name,cfg):
        nonlocal run_count
        run_count+=1;p=out/name
        if pathlib.Path(str(p)+".config.json").exists(): raise FileExistsError("Archive the existing run directory before rerunning: "+str(p))
        pathlib.Path(str(p)+".config.json").write_text(json.dumps(cfg,indent=2)+"\n")
        env=dict(os.environ)
        if args.sanitize:env.update(ASAN_OPTIONS="detect_leaks=1:abort_on_error=1",UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1")
        result=subprocess.run(["taskset","-c",str(cpu),str(binary),str(pathlib.Path(str(p)+".config.json")),str(pathlib.Path(str(p)+".json"))],capture_output=True,text=True,timeout=90,env=env)
        pathlib.Path(str(p)+".stderr").write_text(result.stderr)
        if result.returncode:raise RuntimeError(f"{name}: {result.returncode}: {result.stderr}")
        result_bytes=pathlib.Path(str(p)+".json").read_bytes()
        data=json.loads(result_bytes)
        case_manifest.append({"name":name,"config_sha256":hashlib.sha256(pathlib.Path(str(p)+".config.json").read_bytes()).hexdigest(),"result_sha256":hashlib.sha256(result_bytes).hexdigest()})
        if not cfg.get("estimator"):
            assert data["allocation_bytes"]==data["completion_bytes"]
            assert all(s["inflight"]==0 for s in data["final_stats"])
            assert data["allocation_samples"]==cfg["requests"]-cfg.get("warmup_requests",0)
        return data
    def check(name,value):
        checks.append({"name":name,"passed":bool(value)})
        assert value,name
        print("CHECK",name,"PASS",flush=True)
    def config(alpha,load,seed):
        return {**BASE,"alpha":alpha,"seed":seed,"offered_fraction":load,
                "arrival_interval_ns":round(1048576/(25e9*load)*1e9),"request_trace_from":4000,"request_trace_to":4200}
    def row(data,name):
        s=summarize(data);c=data["config"]
        s.update(name=name,alpha=c["alpha"],allocation_step=data["allocation_step"],
                 allocation_std=data["allocation_rail0_std"],balanced_fraction=data["allocation_histogram"][8]/data["allocation_samples"],
                 allocation_histogram=data["allocation_histogram"],request_decisions=data["request_decisions"])
        measured=[w for w in data["windows"] if w["start_ns"]>=c["warmup_requests"]*c["arrival_interval_ns"]]
        assigned=[sum(w["assigned"][i] for w in measured) for i in range(2)]
        s["steady_share0"]=assigned[0]/sum(assigned)
        s["steady_ewma"]=[statistics.mean(w["bandwidth"][i] for w in measured) for i in range(2)]
        s["effective_utilization"]=s["goodput_gbps"]*1e9/8/sum(c["capacity_bytes_per_second"])
        return s
    def event(at,rate):return {"at_ns":at,"rail":0,"bytes_per_second":rate}
    cfg={**BASE,"requests":1000,"warmup_requests":200,"arrival_interval_ns":50000}
    plain=execute("validate_plain",cfg)
    null=execute("validate_null_event",{**cfg,"capacity_events":[event(123456,12.5e9)]})
    check("unchanged_capacity_preserves_digest",plain["digest"]==null["digest"])
    old=json.loads((OUTPUT_ROOT/"runs/experiment-a/run/validate_observed.json").read_text())
    check("A_driver_parity",plain["digest"]==old["digest"])
    traced=execute("validate_decision_trace",{**cfg,"request_trace_from":200,"request_trace_to":300})
    check("request_trace_does_not_change_policy",plain["digest"]==traced["digest"] and len(traced["request_decisions"])==100)
    rr=execute("validate_rr",{**cfg,"smart":False})
    check("balanced_rr_has_zero_allocation_variation",rr["allocation_step"]==0 and rr["allocation_rail0_std"]==0)
    analytic={**BASE,"rails":1,"capacity_bytes_per_second":[12.5e9],"requests":1,"warmup_requests":0,
              "request_bytes":65536,"arrival_interval_ns":100000,"window_ns":0,"poll_interval_ns":1}
    drop=execute("validate_crossing_drop",{**analytic,"capacity_events":[event(1000,6.25e9)]})
    both=execute("validate_crossing_restore",{**analytic,"capacity_events":[event(1000,6.25e9),event(2000,12.5e9)]})
    check("service_integrates_inflight_drop",drop["p99_ns"]==10486)
    check("service_integrates_inflight_recovery",both["p99_ns"]==6743)
    static=execute("validate_static_slow",{**cfg,"capacity_bytes_per_second":[6.25e9,12.5e9]})
    initial=execute("validate_initial_slow",{**cfg,"capacity_events":[event(0,6.25e9)]})
    check("constant_and_scheduled_capacity_equivalent",static["digest"]==initial["digest"])
    for alpha in ALPHAS:
        e=execute(f"validate_estimator_{alpha}",{"estimator":True,"alpha":alpha,"seed":1,"samples":10000})
        check(f"step95_{alpha}",e["up_step95_samples"]==e["down_step95_samples"]==e["expected_step95_samples"] and e["clamped_samples"]==0)
    check("distinct_decimal_alpha_artifacts",all((out/f"validate_estimator_{a}.json").exists() for a in ALPHAS))
    (out/"validation.json").write_text(json.dumps(checks,indent=2)+"\n")
    if args.validate_only or args.sanitize:
        (out/"case-manifest.json").write_text(json.dumps(case_manifest,indent=2)+"\n")
        print("B_VALIDATED",len(checks),flush=True);return
    estimator=[]; steady=[]; calibration=[]; dynamic=[]
    for alpha in ALPHAS:
        for seed in SEEDS:
            e=execute(f"estimator_{alpha}_{seed}",{"estimator":True,"alpha":alpha,"seed":seed,"samples":1000000})
            estimator.append(e)
        samples=[e for e in estimator if e["alpha"]==alpha]
        relative=statistics.median(e["variance_ratio"]/e["theory_ratio"]-1 for e in samples)
        check(f"stationary_variance_theory_{alpha}",abs(relative)<.1 and all(e["clamped_samples"]==0 for e in samples))
    (out/"estimator.json").write_text(json.dumps(estimator,indent=2)+"\n")
    matrix=[(a,l,s) for a in ALPHAS for l in LOADS for s in SEEDS]
    random.Random(9082).shuffle(matrix)
    for index,(alpha,load,seed) in enumerate(matrix):
        cfg=config(alpha,load,seed);stem=f"a{alpha}_l{int(load*100)}_s{seed}"
        name="steady_"+stem;data=execute(name,cfg);baseline=row(data,name);steady.append(baseline)
        if alpha==.01:
            oldpath=OUTPUT_ROOT/f"runs/experiment-a/run/equal_1048576_{int(load*100)}_{seed}_smart.json"
            check("A_full_parity_"+stem,data["digest"]==json.loads(oldpath.read_text())["digest"])
        if load<.75:
            name="calibration_"+stem
            cal=row(execute(name,{**cfg,"capacity_bytes_per_second":[6.25e9,12.5e9]}),name)
            calibration.append(cal)
        else:cal=None
        t0=cfg["warmup_requests"]*cfg["arrival_interval_ns"]+50000000
        n=math.ceil((t0+300000000)/cfg["arrival_interval_ns"])
        controlcfg={**cfg,"requests":n}
        control=execute("control_"+stem,controlcfg)
        for scenario in ["pulse1ms","held_drop","drop100ms_recover"]:
            schedule=[event(t0,6.25e9)]
            if scenario!="held_drop":schedule.append(event(t0+(1000000 if scenario=="pulse1ms" else 100000000),12.5e9))
            name=scenario+"_"+stem
            changed=execute(name,{**controlcfg,"capacity_events":schedule})
            check_before=[i for i,t in enumerate(control["request_latency_ns"]) if i*cfg["arrival_interval_ns"]+t<t0]
            assert all(changed["request_latency_ns"][i]==control["request_latency_ns"][i] for i in check_before)
            startidx=max(0,math.floor((t0-max(control["request_latency_ns"]))/cfg["arrival_interval_ns"]))
            endidx=min(n,math.ceil((t0+20000000)/cfg["arrival_interval_ns"]))
            extra=max(changed["request_latency_ns"][i]-control["request_latency_ns"][i] for i in range(startidx,endidx))
            ws=[w for w in changed["windows"] if t0<=w["start_ns"]<t0+300000000]
            control_ws={w["start_ns"]:w for w in control["windows"]}
            def share(w):return w["assigned"][0]/sum(w["assigned"]) if sum(w["assigned"]) else None
            flow_excursion=max(abs(share(w)-share(control_ws[w["start_ns"]])) for w in ws if w["start_ns"]<t0+20000000 and share(w) is not None)
            def settle(at,end,target):
                bins=[w for w in ws if at<=w["start_ns"] and w["start_ns"]+1000000<=end]
                for j in range(len(bins)-9):
                    if all(share(w) is not None and abs(share(w)-target)<=.05 for w in bins[j:j+10]):
                        return (bins[j]["start_ns"]+1000000-at)/1e6
                return None
            down_end=t0+(100000000 if scenario=="drop100ms_recover" else 300000000)
            down_settle=settle(t0,down_end,cal["steady_share0"]) if cal and scenario!="pulse1ms" else None
            restore=t0+(1000000 if scenario=="pulse1ms" else 100000000)
            recovery_settle=settle(restore,t0+300000000,baseline["steady_share0"]) if scenario!="held_drop" else None
            last=[w for w in ws if w["start_ns"]>=t0+200000000]
            goodput=sum(sum(w["done"]) for w in last)/(len(last)*.001)*8/1e9
            i0=math.ceil(t0/cfg["arrival_interval_ns"])
            region=changed["request_latency_ns"][i0:]
            d={"name":name,"alpha":alpha,"load":load,"seed":seed,"scenario":scenario,"event_ns":t0,
               "degraded_phase_overloaded":load>.75,"extra_peak_latency_us":extra/1000,
               "flow_share_excursion":flow_excursion,"down_settle_ms":down_settle,"recovery_settle_ms":recovery_settle,
               "post_event_p99_us":sorted(region)[math.ceil(.99*len(region))-1]/1000,
               "last_100ms_goodput_gbps":goodput,"max_queue_bytes":max(sum(w["queue_bytes"]) for w in ws),
               "flow_target_degraded":cal["steady_share0"] if cal else None,"flow_target_healthy":baseline["steady_share0"],
               "pre_event_prefix_equal":True}
            dynamic.append(d)
        if (index+1)%5==0:print("B_MATRIX_PROGRESS",index+1,"/75",flush=True)
    check("one_distinct_artifact_pair_per_run",len({r["name"] for r in case_manifest})==run_count and all((out/(r["name"]+".json")).exists() for r in case_manifest))
    (out/"case-manifest.json").write_text(json.dumps(case_manifest,indent=2)+"\n")
    result={"estimator":estimator,"steady":steady,"calibration":calibration,"dynamic":dynamic,
            "checks":checks,"process_runs":run_count,"closed_loop_runs":len(steady)+len(calibration)+len(dynamic)+len(matrix),
            "paired_controls":len(matrix),"seeds":SEEDS,"alphas":ALPHAS,"loads":LOADS,
            "settling_definition":"1ms full bins, share within 5 percentage points of separately calibrated same-alpha steady share for 10 consecutive bins; entry is end of first qualifying bin",
            "null_settle":"not attained within horizon, not applicable for overload/drop-free phase, or pulse; inspect scenario",
            "binary_sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),
            "build":json.loads((binary.parent/"manifest.json").read_text())}
    core=[ROOT/"simulator/driver.cpp",ROOT/"simulator/hooks.h",ROOT/"scripts/build.py",ROOT/"scripts/run_experiment_a.py",pathlib.Path(__file__)]
    result["source_sha256"]={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in core}
    (out/"summary.json").write_text(json.dumps(result,indent=2)+"\n")
    (out/"validation.json").write_text(json.dumps(checks,indent=2)+"\n")
    print("EXPERIMENT_B_DONE",len(steady),len(calibration),len(dynamic),len(checks),run_count,flush=True)


if __name__=="__main__":main()
