#!/usr/bin/env python3
"""Compile real, pinned selector sources; fail if either hook anchor changes."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT, REPO_ROOT
import argparse
import difflib
import hashlib
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = REPO_ROOT
TENT = REPO / "mooncake-transfer-engine/tent"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sanitize", action="store_true")
    args = parser.parse_args()
    build = OUTPUT_ROOT / "build" / ("sanitize" if args.sanitize else "release")
    build.mkdir(parents=True, exist_ok=True)
    quota = TENT / "src/transport/rdma/quota.cpp"
    original = quota.read_text()
    transport = TENT / "src/transport/rdma/rdma_transport.cpp"
    original_transport = transport.read_text()
    begin = original_transport.index("        const double merge_ratio = 0.25;")
    end = original_transport.index("        std::vector<int> slice_dev_ids;", begin)
    block = original_transport[begin:end].replace("request.length", "length")
    assert "num_slices >= max_slice_count / 2" in original_transport
    header = ("#pragma once\n#include <sys/param.h>\n"
              "struct ProductionSplit { uint64_t count, bytes; bool aggregate; };\n"
              "inline ProductionSplit productionSplit(uint64_t length) {\n"
              "const uint64_t default_block_size = 65536;\n"
              "const uint64_t max_slice_count = 32; // CPU WRITE branch\n"
              + block + "return {num_slices, block_size, num_slices >= max_slice_count / 2};\n}\n")
    (build / "production_split.h").write_text(header)
    (build / "production_split_origin.txt").write_text(original_transport[begin:end])
    clock_anchor = "uint64_t now = getCurrentTimeInNano();"
    trace_anchor = "    return Status::OK();\n}\n\nvoid DeviceSelector::selectSinglePath("
    assert original.count(clock_anchor) == 1
    assert original.count(trace_anchor) == 1
    virtual = '#include "hooks.h"\n' + original.replace(
        clock_anchor, "uint64_t now = experiment::now_ns;"
    )
    traced = virtual.replace(trace_anchor,
        "    if ((experiment::observe && experiment::wanted) || experiment::aggregate)\n"
        "        experiment::candidates(candidates, slice_bytes, request_priority);\n"
        + trace_anchor)
    sample_anchor="    double current_ewma = dev.getEwmaBandwidth();"
    alpha_anchor="    double alpha = sched_params_.bandwidth_learning_rate;"
    assert original.count(sample_anchor)==1 and original.count(alpha_anchor)==1
    controlled=traced.replace(sample_anchor,
        "    double gated_alpha = sched_params_.bandwidth_learning_rate;\n"
        "    if (!experiment::sampleGate(dev_id, observed_bw, gated_alpha)) return Status::OK();\n"
        + sample_anchor).replace(alpha_anchor,"    double alpha = gated_alpha;")
    (build/"sampling.patch").write_text("".join(difflib.unified_diff(
        original.splitlines(True),controlled.splitlines(True),fromfile="quota.cpp",tofile="quota.controlled.cpp")))
    (build / "observer.patch").write_text("".join(difflib.unified_diff(
        original.splitlines(True), traced.splitlines(True),
        fromfile="quota.cpp", tofile="quota.observed.cpp")))
    sources = [TENT / "src/runtime/topology.cpp",
               TENT / "src/common/status.cpp",
               TENT / "src/transport/rdma/gdr_reachability.cpp"]
    flags = ["g++", "-std=c++17", "-O2", "-g", "-ffunction-sections",
             "-fdata-sections", "-I" + str(TENT / "include"),
             "-I" + str(ROOT / "simulator"), "-I" + str(build), "-pthread"]
    if args.sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    commands = []
    common = []
    for src in sources:
        obj = build / (src.stem + ".o")
        cmd = flags + ["-c", str(src), "-o", str(obj)]
        subprocess.run(cmd, check=True)
        commands.append(cmd)
        common.append(str(obj))
    for name, contents in [("native", original), ("clock", virtual),
                           ("observed", traced), ("controlled", controlled)]:
        src = build / ("quota." + name + ".cpp")
        src.write_text(contents)
        cmd = flags + [str(ROOT / "simulator/driver.cpp"), str(src)] + common + [
            "-Wl,--gc-sections", "-lglog", "-lnuma", "-o", str(build / name)]
        subprocess.run(cmd, check=True)
        commands.append(cmd)
    tracked_sources = [quota, transport, *sources,
                       TENT / "include/tent/transport/rdma/quota.h",
                       TENT / "include/tent/common/utils/random.h"]
    manifest = {"source_sha256": {str(p.relative_to(REPO)):
                hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked_sources},
                "commands": commands,
                "compiler": subprocess.check_output(["g++", "--version"], text=True),
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()}
    (build / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("BUILD_OK", build, flush=True)


if __name__ == "__main__":
    main()
