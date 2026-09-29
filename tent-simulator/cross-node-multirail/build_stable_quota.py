#!/usr/bin/env python3
"""Build private SQ allocation changes against the frozen O transport."""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from build_oscillation import BASE, REPO, once, sha

OUT = BASE / 'build-stable-quota-20260929'


def main():
    old, out, here = BASE / 'build-oscillation-20260917', OUT, Path(__file__).resolve().parent
    if out.exists():
        raise ValueError('fresh build required')
    m = json.loads((old / 'build-manifest.json').read_text())
    for name in ('quota.cpp', 'observer.cpp', 'observer.h', 'osc_trace.h', 'osc_trace.cpp', 'libtent_xport_rdma.a'):
        if sha(old / name) != m['outputs'][name]:
            raise ValueError('frozen O source differs: ' + name)
    out.mkdir()
    for name in ('stable_quota.h', 'stable_quota.cpp'):
        shutil.copy2(here / name, out / name)
    quota = '#include "stable_quota.h"\n' + (old / 'quota.cpp').read_text()
    quota = once(quota, '        info.dev_id = dev_id;',
        '        info.dev_id = dev_id;\n        ::tent_sq::bind(dev_id, local_topology_->getNicName(dev_id).c_str());')
    quota = once(quota, '        candidates.push_back(c);',
        '        candidates.push_back(c);\n        ::tent_sq::capture(dev_id, inflight);')
    new = '''    if (tent_sq_policy() && !probe_mode && candidates.size() == 2 &&
        num_slices == 16 && total_length == 1048576) {
        const size_t lo = candidates[0].dev_id < candidates[1].dev_id ? 0 : 1;
        const auto& a = candidates[lo]; const auto& b = candidates[lo ^ 1];
        const auto plan = ::tent_sq::choose(a.dev_id, b.dev_id, ::tent_obs::decision_stamp());
        const uint32_t counts[2] = {plan.first, num_slices - plan.first};
        TENT_OBS_ALLOC(::tent_obs::weight(a.dev_id, plan.weight, 1.0));
        TENT_OBS_ALLOC(::tent_obs::weight(b.dev_id, 1.0 - plan.weight, 1.0));
        for (size_t j = 0; j < 2; ++j) {
            const auto& c = candidates[lo ^ j];
            for (uint32_t k = 0; k < counts[j]; ++k) {
                slice_dev_ids.push_back(c.dev_id);
                if (slice_charged_bytes) slice_charged_bytes->push_back(slice_bytes);
            }
            const uint64_t bytes = slice_bytes * counts[j];
            devices_[c.dev_id].addInflight(bytes);
            devices_[c.dev_id].total_bytes.fetch_add(bytes, std::memory_order_relaxed);
        }
        return;
    }
'''
    quota = once(quota, '    if (tent_osc_fixed() && !probe_mode) {', new + '    if (tent_osc_fixed() && !probe_mode) {')
    (out / 'quota.cpp').write_text(quota)
    (out / 'observer.h').write_text((old / 'observer.h').read_text() + '\nnamespace tent_obs { uint64_t decision_stamp() noexcept; }\n')
    observer = '#include "stable_quota.h"\n' + (old / 'observer.cpp').read_text()
    observer = once(observer, 'void weight(int dev, double inverse_score, double total_weight) noexcept {',
        'uint64_t decision_stamp() noexcept { auto* p = pending(); return p ? p->stamp : clock_ns(); }\n'
        'void weight(int dev, double inverse_score, double total_weight) noexcept {')
    observer = once(observer, '(tent_osc_fixed() ? "shadow_inverse_score" : "consumed_inverse_score")',
        '(tent_sq_policy() ? "consumed_stable_quota" : "consumed_inverse_score")')
    (out / 'observer.cpp').write_text(observer)
    for name in ('osc_trace.h', 'osc_trace.cpp'):
        shutil.copy2(old / name, out / name)
    base = REPO / 'build-tent'
    flags = base / 'mooncake-transfer-engine/tent/src/transport/rdma/CMakeFiles/tent_xport_rdma.dir/flags.make'
    values = dict(line.split(' = ', 1) for line in flags.read_text().splitlines() if ' = ' in line)
    options = sum((shlex.split(values[k]) for k in ('CXX_DEFINES', 'CXX_INCLUDES', 'CXX_FLAGS')), []) + ['-I' + str(out)]
    for name in ('quota', 'observer', 'osc_trace', 'stable_quota'):
        subprocess.run(['/usr/bin/c++', *options, '-c', str(out / (name + '.cpp')), '-o', str(out / (name + '.cpp.o'))], check=True)
    archive = out / 'libtent_xport_rdma.a'; shutil.copy2(old / archive.name, archive)
    subprocess.run(['ar', 'r', str(archive), str(out / 'quota.cpp.o')], check=True)
    subprocess.run(['ranlib', str(archive)], check=True)
    work = base / 'mooncake-transfer-engine/tent/src'
    command = shlex.split((work / 'CMakeFiles/tent_shared.dir/link.txt').read_text())
    command[command.index('-o') + 1] = str(out / 'libtent_shared.so')
    command = [str(archive) if x == 'transport/rdma/libtent_xport_rdma.a' else x for x in command]
    command[1:1] = [str(out / (n + '.cpp.o')) for n in ('observer', 'osc_trace', 'stable_quota')]
    subprocess.run(command, cwd=work, check=True)
    (out / 'build-manifest.json').write_text(json.dumps(dict(parent_O_sha256=sha(old / 'build-manifest.json'),
        source={n: sha(here / n) for n in ('stable_quota.h', 'stable_quota.cpp', 'build_stable_quota.py')},
        outputs={p.name: sha(p) for p in out.iterdir() if p.is_file()}, command=command), indent=2))
    print('STABLE_QUOTA_BUILD_OK', out, flush=True)


if __name__ == '__main__':
    main()
