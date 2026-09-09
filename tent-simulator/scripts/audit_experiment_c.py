#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT, REPO_ROOT
import hashlib
import json
import pathlib
import subprocess

ROOT=pathlib.Path(__file__).resolve().parents[1]


def main():
    run=OUTPUT_ROOT/"runs/experiment-c/run"
    data=json.loads((run/"summary.json").read_text())
    cases=json.loads((run/"case-manifest.json").read_text())
    assert len(cases)==data["case_count"]==580 and len({c["name"] for c in cases})==580
    for c in cases:
        assert hashlib.sha256((run/(c["name"]+".config.json")).read_bytes()).hexdigest()==c["input_sha256"]
        assert hashlib.sha256((run/(c["name"]+".json")).read_bytes()).hexdigest()==c["result_sha256"]
    assert all(hashlib.sha256((ROOT/k).read_bytes()).hexdigest()==v for k,v in data["source_sha256"].items())
    assert hashlib.sha256((OUTPUT_ROOT/"build/release/controlled").read_bytes()).hexdigest()==data["binary_sha256"]
    subprocess.run(["git","diff","--quiet","1c65ced88e509440e31ea88a76594660461d0eab","--","mooncake-transfer-engine/tent"],cwd=REPO_ROOT,check=True)
    result={"verified_case_pairs":580,"source_and_binary_verified":True,"production_unchanged":True}
    (run/"material-audit.json").write_text(json.dumps(result,indent=2)+"\n")
    print("C_MATERIAL_AUDIT",result,flush=True)


if __name__=="__main__":main()
