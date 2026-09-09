#!/usr/bin/env python3
"""Read-only P0 snapshot. No instance metadata credentials or environment dump."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT, REPO_ROOT, external_output
import argparse
import datetime
import hashlib
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = REPO_ROOT
COMMANDS = {
    "git": ["git", "status", "--short"],
    "branch": ["git", "branch", "--show-current"],
    "head": ["git", "rev-parse", "HEAD"],
    "kernel": ["uname", "-a"],
    "cpu": ["lscpu"],
    "numa": ["numactl", "--hardware"],
    "gpu_topology": ["nvidia-smi", "topo", "-m"],
    "gpu_state": ["nvidia-smi", "--query-gpu=name,pci.bus_id,driver_version,utilization.gpu,memory.used", "--format=csv"],
    "gpu_processes": ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv"],
    "rdma_link": ["rdma", "link", "show"],
    "rdma_devices": ["ibv_devinfo"],
    "modules": ["lsmod"],
    "load": ["uptime"],
    "process_load": ["ps", "-eo", "pid,comm,pcpu,pmem", "--sort=-pcpu"],
    "compiler": ["g++", "--version"],
}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=pathlib.Path)
    args=parser.parse_args()
    out = external_output(args.output_dir or OUTPUT_ROOT / "runs/p01")
    out.mkdir(parents=True, exist_ok=True)
    result = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "commands": {}}
    for name, cmd in COMMANDS.items():
        try:
            p = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True, timeout=30)
            result["commands"][name] = {"argv": cmd, "exit_code": p.returncode,
                                         "stdout": p.stdout, "stderr": p.stderr}
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["commands"][name] = {"argv": cmd, "error": str(exc)}
    result["rdma_sysfs"] = {}
    for dev in pathlib.Path("/sys/class/infiniband").glob("*"):
        node = dev / "device/numa_node"
        result["rdma_sysfs"][dev.name] = {"device": str((dev / "device").resolve()),
            "numa": node.read_text().strip() if node.exists() else None}
    paths = ["mooncake-transfer-engine/tent/src/transport/rdma/" + n
             for n in ["quota.cpp", "workers.cpp", "rdma_transport.cpp", "rail_monitor.cpp"]]
    result["source_sha256"] = {p: hashlib.sha256((REPO/p).read_bytes()).hexdigest() for p in paths}
    result["production_diff"] = subprocess.check_output(
        ["git", "diff", "1c65ced88e509440e31ea88a76594660461d0eab", "--", "mooncake-transfer-engine/tent"],
        cwd=REPO, text=True)
    (out / "environment.json").write_text(json.dumps(result, indent=2) + "\n")
    print("ENVIRONMENT_SAVED", out / "environment.json", flush=True)


if __name__ == "__main__":
    main()
