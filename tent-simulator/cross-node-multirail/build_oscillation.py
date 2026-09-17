#!/usr/bin/env python3
"""SSH-only: private trace and fixed-share controls; leave production untouched."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess

BASE = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')
REPO = Path('/root/mooncake')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('source anchor mismatch: ' + old[:100])
    return text.replace(old, new)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'build-oscillation-20260917')
    args = parser.parse_args()
    out = args.output.resolve()
    if out.exists() or REPO == out or REPO in out.parents:
        raise ValueError('output must be new and outside the repository')
    here = Path(__file__).resolve().parent
    observed, base = BASE / 'build-v2', REPO / 'build-tent'
    if sha(observed / 'libtent_shared.so') != '858e3afaa3cbef4773eeafc9ce8e3e8db1701c02e71fe5b6ac17a575e9528aee':
        raise ValueError('frozen V2 library differs')
    provenance = json.loads((observed / 'source/observer-manifest.json').read_text())
    for name in ('quota.cpp', 'observer.cpp', 'observer.h'):
        expected = (provenance['files'][name]['output_sha256'] if name == 'quota.cpp'
                    else provenance['observer_sha256'][name])
        if sha(observed / 'source' / name) != expected:
            raise ValueError('frozen source differs: ' + name)
    source = REPO / 'mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp'
    if sha(source) != provenance['files']['quota.cpp']['source_sha256']:
        raise ValueError('production quota differs')
    quota = '#include "osc_trace.h"\n#include <cerrno>\n' + (observed / 'source/quota.cpp').read_text()
    quota = once(quota, 'double DeviceSelector::theoreticalBandwidth(const DeviceInfo& dev) const {\n    const auto& p = sched_params_;',
        'double DeviceSelector::theoreticalBandwidth(const DeviceInfo& dev) const {\n'
        '    const double override_bw = tent_osc_bandwidth();\n'
        '    if (override_bw > 0) return override_bw;\n    const auto& p = sched_params_;')
    anchor = '    if (probe_mode) {\n        // Probe mode: round-robin distribution'
    equal = '''    if (tent_osc_fixed() && !probe_mode) {
        if (candidates.size() != 2 || !num_slices || num_slices % 2 ||
            total_length % num_slices) {
            ::tent_osc::reject(EINVAL); // Preserve transport; controller rejects run.
        } else {
            double total = 0;
            for (const auto& c : candidates)
                total += 1.0 / (c.score + sched_params_.score_epsilon);
            for (const auto& c : candidates) {
                double inverse = 1.0 / (c.score + sched_params_.score_epsilon);
                TENT_OBS_ALLOC(::tent_obs::weight(c.dev_id, inverse, total));
            }
            const size_t first = candidates[0].dev_id < candidates[1].dev_id ? 0 : 1;
            for (size_t j = 0; j < 2; ++j) {
                const auto& c = candidates[first ^ j];
                for (uint32_t n = 0; n < num_slices / 2; ++n) {
                    slice_dev_ids.push_back(c.dev_id);
                    if (slice_charged_bytes) slice_charged_bytes->push_back(slice_bytes);
                }
                const uint64_t bytes = slice_bytes * (num_slices / 2);
                devices_[c.dev_id].addInflight(bytes);
                devices_[c.dev_id].total_bytes.fetch_add(bytes, std::memory_order_relaxed);
            }
            return;
        }
    }
'''
    quota = once(quota, anchor, equal + anchor)
    observer = '#include "osc_trace.h"\n' + (observed / 'source/observer.cpp').read_text()
    anchor = '            if (p.ok && offset == p.key.total && p.count &&\n'
    trace = '''            if (p.ok && offset == p.key.total && p.key.total &&
                p.count == 2 && weights_valid && p.key.mode < 3) {
                ::tent_osc::Record record{};
                record.ns = p.stamp;
                record.total_bytes = p.key.total;
                record.selector = p.key.selector;
                record.slices = p.key.slices;
                record.mode = p.key.mode;
                record.context = ctx;
                record.site = p.key.site;
                for (size_t i = 0; i < 2; ++i) {
                    const auto& a = p.samples[i];
                    record.dev[i] = a.dev;
                    record.inflight[i] = a.inflight;
                    record.assigned[i] = static_cast<uint64_t>(mass[i]);
                    record.score[i] = a.score;
                    record.bandwidth[i] = a.bandwidth;
                    record.penalty[i] = a.penalty;
                    record.weight[i] = weights[i];
                }
                ::tent_osc::append(record);
            }
'''
    observer = once(observer, anchor, trace + anchor)
    observer = once(observer, 'k.mode == 1 ? "consumed_inverse_score"',
                    'k.mode == 1 ? (tent_osc_fixed() ? "shadow_inverse_score" : "consumed_inverse_score")')
    out.mkdir(parents=True)
    (out / 'quota.cpp').write_text(quota)
    (out / 'observer.cpp').write_text(observer)
    shutil.copy2(observed / 'source/observer.h', out / 'observer.h')
    for name in ('osc_trace.cpp', 'osc_trace.h'):
        shutil.copy2(here / name, out / name)
    flags = base / 'mooncake-transfer-engine/tent/src/transport/rdma/CMakeFiles/tent_xport_rdma.dir/flags.make'
    values = dict(line.split(' = ', 1) for line in flags.read_text().splitlines() if ' = ' in line)
    options = sum((shlex.split(values[k]) for k in ('CXX_DEFINES', 'CXX_INCLUDES', 'CXX_FLAGS')), []) + ['-I' + str(out)]
    run = lambda cmd, cwd=None: subprocess.run(cmd, cwd=cwd, check=True)
    for name in ('quota', 'observer', 'osc_trace'):
        run(['/usr/bin/c++', *options, '-c', str(out / (name + '.cpp')), '-o', str(out / (name + '.cpp.o'))])
    archive = out / 'libtent_xport_rdma.a'
    shutil.copy2(observed / 'libtent_xport_rdma.a', archive)
    run(['ar', 'r', str(archive), str(out / 'quota.cpp.o')]); run(['ranlib', str(archive)])
    work = base / 'mooncake-transfer-engine/tent/src'
    command = shlex.split((work / 'CMakeFiles/tent_shared.dir/link.txt').read_text())
    command[command.index('-o') + 1] = str(out / 'libtent_shared.so')
    command = [str(archive) if x == 'transport/rdma/libtent_xport_rdma.a' else x for x in command]
    command[1:1] = [str(out / 'observer.cpp.o'), str(out / 'osc_trace.cpp.o')]
    run(command, cwd=work)
    manifest = dict(scope='O experiment; trace and assignment controls only',
        head=subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip(),
        inputs={str(p): sha(p) for p in [source, observed / 'libtent_shared.so',
            observed / 'libtent_xport_rdma.a', observed / 'source/observer.cpp', flags,
            here / 'osc_trace.h', here / 'osc_trace.cpp', here / 'build_oscillation.py']},
        outputs={p.name: sha(p) for p in out.iterdir() if p.is_file()}, link_command=command)
    (out / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('OSCILLATION_BUILD_COMPLETE', out, flush=True)


if __name__ == '__main__':
    main()
