#!/usr/bin/env python3
"""Extract a warmed, consecutive 100-request decision trace without changing policy."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import collections
import gzip
import json
import pathlib
import subprocess

ROOT=pathlib.Path(__file__).resolve().parents[1]


def main():
    out=OUTPUT_ROOT/"runs/experiment-a/run"
    results=[]
    for load in [20,60,90]:
        name=f"equal_1048576_{load}_1_smart"
        cfg=json.loads((out/(name+".config.json")).read_text())
        cfg.update(requests=4100,observe=True,trace_every=1,trace_limit=220000)
        path=out/("focus_"+name)
        path.with_suffix(".config.json").write_text(json.dumps(cfg,indent=2)+"\n")
        cmd=["taskset","-c","8",str(OUTPUT_ROOT/"build/release/observed"),str(path.with_suffix(".config.json")),str(path.with_suffix(".json"))]
        subprocess.run(cmd,check=True,timeout=90)
        data=json.loads(path.with_suffix(".json").read_text())
        original=json.loads((out/(name+".json")).read_text())
        assert data["request_latency_ns"]==original["request_latency_ns"][:4100]
        assert data["trace_dropped"]==0
        candidate={}; assigned=collections.defaultdict(lambda:[0,0]); post={}; completions=[]
        for row in data["trace"]:
            req=row["request"]
            if req<4000: continue
            if row["event"]=="candidates": candidate[req]=row
            elif row["event"]=="allocate": assigned[req][row["rail"]]+=1
            elif row["event"]=="post": post[(req,row["slice"])]=row["time_ns"]
            elif row["event"]=="complete": completions.append(row)
        assert len(candidate)==100 and len(completions)==1600
        rows=[]
        for req in sorted(candidate):
            c=candidate[req]
            p0=next(w["p"] for w in c["weights"] if w["rail"]==0)
            rows.append({"request":req,"time_ns":c["time_ns"],"p0":p0,
                "inflight":[r["inflight"] for r in c["stats_before"]],
                "bandwidth":[r["ewma"] for r in c["stats_before"]],
                "assigned_slices":assigned[req],"latency_ns":data["request_latency_ns"][req]})
        results.append({"load":load,"prefix_latency_matches_matrix":True,"trace_dropped":0,
                        "allocation_counts":dict(collections.Counter(str(r["assigned_slices"]) for r in rows)),
                        "rows":rows})
        with gzip.open(path.with_suffix(".json.gz"),"wb") as stream:
            stream.write(path.with_suffix(".json").read_bytes())
        path.with_suffix(".json").unlink()
        print("FOCUS_VALIDATED",load,results[-1]["allocation_counts"],flush=True)
    (out/"focus.json").write_text(json.dumps(results,indent=2)+"\n")


if __name__=="__main__": main()
