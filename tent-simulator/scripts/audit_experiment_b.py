#!/usr/bin/env python3
"""Verify raw artifacts and whether the undisturbed reference meets recovery bands."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import hashlib
import json
import pathlib

ROOT=pathlib.Path(__file__).resolve().parents[1]


def main():
    root=OUTPUT_ROOT/"runs/experiment-b";run=root/"run"
    data=json.loads((run/"summary.json").read_text())
    cases=json.loads((run/"case-manifest.json").read_text())
    assert len(cases)==data["process_runs"]==463
    for c in cases:
        for ext,key in [(".config.json","config_sha256"),(".json","result_sha256")]:
            assert hashlib.sha256((run/(c["name"]+ext)).read_bytes()).hexdigest()==c[key]
    assert len({c["name"] for c in cases})==len(cases)
    assert all(hashlib.sha256((ROOT/k).read_bytes()).hexdigest()==v for k,v in data["source_sha256"].items())
    assert hashlib.sha256((OUTPUT_ROOT/"build/release/observed").read_bytes()).hexdigest()==data["binary_sha256"]
    oldpath=root/"run-filename-collision/summary.json"
    same={}
    if oldpath.exists():
        old=json.loads(oldpath.read_text())
        same={k:data[k]==old[k] for k in ["estimator","steady","calibration","dynamic"]}
        assert all(same.values())
    references=[]
    for d in data["dynamic"]:
        if d["scenario"]=="held_drop":continue
        stem=d["name"][len(d["scenario"])+1:]
        control=json.loads((run/("control_"+stem+".json")).read_text())
        at=d["event_ns"]+(1000000 if d["scenario"]=="pulse1ms" else 100000000)
        bins=[w for w in control["windows"] if w["start_ns"]>=at]
        valid=[sum(w["assigned"])>0 and abs(w["assigned"][0]/sum(w["assigned"])-d["flow_target_healthy"])<=.05 for w in bins]
        qualified=any(all(valid[i:i+10]) for i in range(len(valid)-9))
        references.append({"alpha":d["alpha"],"load":d["load"],"seed":d["seed"],"scenario":d["scenario"],
                           "unperturbed_reference_qualifies":qualified,"fraction_in_band":sum(valid)/len(valid)})
    audit={"verified_case_pairs":len(cases),"aggregate_reproduction":same,"reference_self_checks":references}
    (run/"material-audit.json").write_text(json.dumps(audit,indent=2)+"\n")
    (OUTPUT_ROOT/"reports/experiment-b-reference-audit.json").write_text(json.dumps(references,indent=2)+"\n")
    selected=[r for r in references if r["alpha"]==.5 and r["load"]==.2 and r["scenario"]=="drop100ms_recover"]
    print("B_MATERIAL_AND_REFERENCE_AUDIT",len(cases),selected,flush=True)


if __name__=="__main__":main()
