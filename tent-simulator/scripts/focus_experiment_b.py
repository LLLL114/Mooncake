#!/usr/bin/env python3
"""Read-only score decomposition at alpha=.999; not a counterfactual rerun."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import gzip
import json
import pathlib
import subprocess

ROOT=pathlib.Path(__file__).resolve().parents[1]


def main():
    out=OUTPUT_ROOT/"runs/experiment-b/run"; rows=[]
    for load in [60,90]:
        name=f"steady_a0.999_l{load}_s1"
        original=json.loads((out/(name+".json")).read_text())
        cfg=original["config"].copy();cfg.update(requests=4100,observe=True,trace_every=1,trace_limit=220000)
        p=out/("focus_"+name)
        pathlib.Path(str(p)+".config.json").write_text(json.dumps(cfg,indent=2)+"\n")
        subprocess.run(["taskset","-c","8",str(OUTPUT_ROOT/"build/release/observed"),str(pathlib.Path(str(p)+".config.json")),str(pathlib.Path(str(p)+".json"))],check=True,timeout=90)
        data=json.loads(pathlib.Path(str(p)+".json").read_text());assert data["trace_dropped"]==0
        assert data["request_latency_ns"]==original["request_latency_ns"][:4100]
        decision={r["request"]:r for r in data["request_decisions"]}
        result=[]
        for r in data["trace"]:
            if r["event"]!="candidates" or r["request"]<4000:continue
            a,b=r["stats_before"]; w=next(x["p"] for x in r["weights"] if x["rail"]==0)
            i0,i1=a["inflight"],b["inflight"];b0,b1=a["ewma"],b["ewma"]
            predicted=(b0/(i0+65536))/(b0/(i0+65536)+b1/(i1+65536))
            assert abs(w-predicted)<.001
            result.append({"request":r["request"],"p0":w,"inflight":[i0,i1],"ewma":[b0,b1],
                "equal_bandwidth_shadow_p0":(i1+65536)/(i0+i1+131072),
                "zero_queue_shadow_p0":b0/(b0+b1),"count0":decision[r["request"]]["count0"]})
        assert len(result)==100
        rows.append({"alpha":.999,"load":load,"prefix_equal":True,"rows":result})
        with gzip.open(pathlib.Path(str(p)+".json.gz"),"wb") as f:f.write(pathlib.Path(str(p)+".json").read_bytes())
        pathlib.Path(str(p)+".json").unlink()
        print("B_FOCUS_OK",load,flush=True)
    (out/"focus.json").write_text(json.dumps(rows,indent=2)+"\n")


if __name__=="__main__":main()
