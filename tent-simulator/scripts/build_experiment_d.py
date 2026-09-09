#!/usr/bin/env python3
"""Build original executables plus isolated, anchor-checked allocation diagnostics."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import argparse
import difflib
import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--sanitize', action='store_true')
    args = p.parse_args()
    subprocess.run([sys.executable, str(ROOT/'scripts/build.py')] +
                   (['--sanitize'] if args.sanitize else []), check=True)
    build = OUTPUT_ROOT/'build'/('sanitize' if args.sanitize else 'release')
    original = (build/'quota.observed.cpp').read_text()
    source = '#include "allocation.h"\n' + original
    def replace(old, new):
        nonlocal source
        assert source.count(old) == 1, old
        source = source.replace(old, new)
    replace('    return Status::OK();\n}\n\nvoid DeviceSelector::selectSinglePath(',
            '    experiment::dCandidates(candidates);\n    return Status::OK();\n}\n\nvoid DeviceSelector::selectSinglePath(')
    replace('        bool probe_mode = ((++tl_call_count % 100) == 0);',
            '''        bool probe_mode = ((++tl_call_count % 100) == 0);
        if (experiment::allocation_mode == 2) probe_mode = false;
        experiment::dBatch(tl_call_count, probe_mode, total_length);
        if (!probe_mode && experiment::allocation_mode >= 3) {
            // Paired greedy diagnostics share argmin and charging. Only mode 4
            // refreshes candidates after each reservation. No service advances.
            uint64_t charge = (total_length + num_slices - 1) / num_slices;
            for (uint32_t s = 0; s < num_slices; ++s) {
                if (s && experiment::allocation_mode == 4) {
                    tl_candidates.clear();
                    status = buildCandidates(entry, slice_bytes, device_mask,
                                             tl_candidates, priority);
                    if (!status.ok()) return status;
                }
                selectSinglePath(tl_candidates, 1, charge, slice_dev_ids,
                                 slice_charged_bytes);
            }
            return Status::OK();
        }''')
    # Keep floors, output order and charge accounting untouched. Only change
    # which candidates receive the remainder. Each candidate gets at most one.
    replace('        if (remaining_slices > 0) {\n            const Candidate& c = candidates[best_dev_idx];',
            '''        if (remaining_slices > 0 && experiment::allocation_mode == 1) {
            std::vector<size_t> order(candidates.size());
            for (size_t i = 0; i < order.size(); ++i) order[i] = i;
            auto fraction = [&](size_t i) {
                double exact = (1.0 / (candidates[i].score + sched_params_.score_epsilon))
                               / total_weight * num_slices;
                return exact - std::floor(exact);
            };
            std::sort(order.begin(), order.end(), [&](size_t a, size_t b) {
                double fa = fraction(a), fb = fraction(b);
                return fa != fb ? fa > fb : candidates[a].dev_id < candidates[b].dev_id;
            });
            for (size_t i = 0; remaining_slices && i < order.size(); ++i) {
                int id = candidates[order[i]].dev_id;
                slice_dev_ids.push_back(id);
                if (slice_charged_bytes) slice_charged_bytes->push_back(slice_bytes);
                devices_[id].addInflight(slice_bytes);
                devices_[id].total_bytes.fetch_add(slice_bytes, std::memory_order_relaxed);
                --remaining_slices;
            }
        }
        if (remaining_slices > 0) {
            const Candidate& c = candidates[best_dev_idx];''')
    src = build/'quota.d.cpp'
    src.write_text(source)
    (build/'allocation.patch').write_text(''.join(difflib.unified_diff(
        original.splitlines(True), source.splitlines(True),
        fromfile='quota.observed.cpp', tofile='quota.d.cpp')))
    manifest = json.loads((build/'manifest.json').read_text())
    cmd = next(c for c in manifest['commands'] if c[-1] == str(build/'observed'))
    cmd = [str(ROOT/'simulator/driver_d.cpp') if c == str(ROOT/'simulator/driver.cpp')
           else str(src) if c == str(build/'quota.observed.cpp')
           else str(build/'allocation') if c == str(build/'observed') else c for c in cmd]
    subprocess.run(cmd, check=True)
    manifest['allocation_command'] = cmd
    manifest['allocation_patch_sha256'] = hashlib.sha256((build/'allocation.patch').read_bytes()).hexdigest()
    (build/'manifest-d.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print('D_BUILD_OK', build, flush=True)

if __name__ == '__main__':
    main()
