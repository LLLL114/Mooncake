#!/usr/bin/env python3
"""Preserve exact experiment inputs including uncommitted files for P0 lineage."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT, REPO_ROOT
import datetime
import hashlib
import json
import pathlib
import shutil
import subprocess

ROOT=pathlib.Path(__file__).resolve().parents[1]
REPO=REPO_ROOT


def main():
    target=OUTPUT_ROOT/"runs/p01/source_snapshot"
    target.mkdir(parents=True,exist_ok=True)
    files={}
    for path in sorted(ROOT.rglob("*")):
        relative=path.relative_to(ROOT)
        if not path.is_file() or relative.parts[0] in {"runs","build","reports"} or "__pycache__" in relative.parts:
            continue
        destination=target/relative
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,destination)
        files[str(relative)]=hashlib.sha256(path.read_bytes()).hexdigest()
    manifest={"utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "branch":subprocess.check_output(["git","branch","--show-current"],cwd=REPO,text=True).strip(),
              "head":subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPO,text=True).strip(),
              "source_sha256":files,
              "build_manifest":json.loads((OUTPUT_ROOT/"build/release/manifest.json").read_text()),
              "scope":"CPU-only P0/P1; no live network or GPU load generation"}
    (OUTPUT_ROOT/"runs/p01/run_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print("RUN_SEALED",len(files),"source files")


if __name__=="__main__":
    main()
