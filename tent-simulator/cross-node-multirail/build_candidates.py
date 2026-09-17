#!/usr/bin/env python3
"""Build the candidate experiment from the frozen O shadow library, SSH only."""
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from build_oscillation import BASE, REPO, once, sha

OUT = BASE / 'build-candidates-20260917'


def main():
    old = BASE / 'build-oscillation-20260917'
    out = OUT
    if out.exists():
        raise ValueError('fresh build directory required')
    manifest = json.loads((old / 'build-manifest.json').read_text())
    for name in ('quota.cpp', 'observer.cpp', 'observer.h', 'osc_trace.h', 'osc_trace.cpp', 'libtent_xport_rdma.a'):
        if sha(old / name) != manifest['outputs'][name]:
            raise ValueError('frozen O input differs: ' + name)
    out.mkdir()
    here = Path(__file__).resolve().parent
    for name in ('candidate_policy.h', 'candidate_policy.cpp'):
        shutil.copy2(here / name, out / name)
    quota = '#include "candidate_policy.h"\n' + (old / 'quota.cpp').read_text()
    anchor = '    if (tent_osc_fixed() && !probe_mode) {'
    new = '''    if (candidates.size() != 2) ::tent_cand::reset();
    if (tent_cand_policy() && !probe_mode && candidates.size() == 2 &&
        num_slices && total_length % num_slices == 0) {
        const size_t lo = candidates[0].dev_id < candidates[1].dev_id ? 0 : 1;
        const auto& a = candidates[lo];
        const auto& b = candidates[lo ^ 1];
        const double ia = 1.0 / (a.score + sched_params_.score_epsilon);
        const double ib = 1.0 / (b.score + sched_params_.score_epsilon);
        const double w = ::tent_cand::choose(ia / (ia + ib),
            ::tent_obs::decision_stamp(), this, a.dev_id, b.dev_id);
        uint32_t count[2];
        ::tent_cand::quotas(w, num_slices, tent_cand_policy() == 3, count[0], count[1]);
        TENT_OBS_ALLOC(::tent_obs::weight(a.dev_id, w, 1.0));
        TENT_OBS_ALLOC(::tent_obs::weight(b.dev_id, 1.0 - w, 1.0));
        for (size_t j = 0; j < 2; ++j) {
            const auto& c = candidates[lo ^ j];
            for (uint32_t n = 0; n < count[j]; ++n) {
                slice_dev_ids.push_back(c.dev_id);
                if (slice_charged_bytes) slice_charged_bytes->push_back(slice_bytes);
            }
            const uint64_t bytes = slice_bytes * count[j];
            devices_[c.dev_id].addInflight(bytes);
            devices_[c.dev_id].total_bytes.fetch_add(bytes, std::memory_order_relaxed);
        }
        return;
    }
'''
    quota = once(quota, anchor, new + anchor)
    (out / 'quota.cpp').write_text(quota)
    header = (old / 'observer.h').read_text() + '\nnamespace tent_obs { uint64_t decision_stamp() noexcept; }\n'
    (out / 'observer.h').write_text(header)
    observer = '#include "candidate_policy.h"\n' + (old / 'observer.cpp').read_text()
    observer = once(observer, 'void weight(int dev, double inverse_score, double total_weight) noexcept {',
        'uint64_t decision_stamp() noexcept { auto* p = pending(); return p ? p->stamp : clock_ns(); }\n'
        'void weight(int dev, double inverse_score, double total_weight) noexcept {')
    observer = once(observer, '                ::tent_osc::append(record);', '''                if (record.mode == 1 && tent_cand_policy()) {
                    const auto s = ::tent_cand::snapshot();
                    record.previous_filtered = s.previous_filtered;
                    record.previous_applied = s.previous_applied;
                    record.filtered = s.filtered;
                    record.previous_ns = s.previous_ns;
                    record.decision_ns = s.decision_ns;
                }
                ::tent_osc::append(record);''')
    observer = once(observer, '(tent_osc_fixed() ? "shadow_inverse_score" : "consumed_inverse_score")',
        '(tent_cand_policy() == 1 || tent_cand_policy() == 2 ? "consumed_filtered_weight" : "consumed_inverse_score")')
    (out / 'observer.cpp').write_text(observer)
    trace_h = once((old / 'osc_trace.h').read_text(),
        '    double score[2], bandwidth[2], penalty[2], weight[2];',
        '    double score[2], bandwidth[2], penalty[2], weight[2];\n'
        '    double previous_filtered, previous_applied, filtered;\n'
        '    uint64_t previous_ns, decision_ns;')
    trace_h = trace_h.replace('sizeof(Record) == 152', 'sizeof(Record) == 192')
    (out / 'osc_trace.h').write_text(trace_h)
    trace = '#include "candidate_policy.h"\n' + (old / 'osc_trace.cpp').read_text()
    trace = trace.replace('tent-oscillation-trace-v1', 'tent-candidate-trace-v1').replace('152', '192')
    trace = trace.replace('<4Q4I2i4Q8d', '<4Q4I2i4Q11d2Q')
    trace = once(trace, '<< ",\\\"capacity_per_thread\\\":" << limit',
        '<< ",\\\"candidate_policy\\\":" << tent_cand_policy()\n             << ",\\\"capacity_per_thread\\\":" << limit')
    (out / 'osc_trace.cpp').write_text(trace)
    base = REPO / 'build-tent'
    flags = base / 'mooncake-transfer-engine/tent/src/transport/rdma/CMakeFiles/tent_xport_rdma.dir/flags.make'
    values = dict(line.split(' = ', 1) for line in flags.read_text().splitlines() if ' = ' in line)
    options = sum((shlex.split(values[k]) for k in ('CXX_DEFINES', 'CXX_INCLUDES', 'CXX_FLAGS')), []) + ['-I' + str(out)]
    for name in ('quota', 'observer', 'osc_trace', 'candidate_policy'):
        subprocess.run(['/usr/bin/c++', *options, '-c', str(out / (name + '.cpp')), '-o', str(out / (name + '.cpp.o'))], check=True)
    archive = out / 'libtent_xport_rdma.a'
    shutil.copy2(old / 'libtent_xport_rdma.a', archive)
    subprocess.run(['ar', 'r', str(archive), str(out / 'quota.cpp.o')], check=True)
    subprocess.run(['ranlib', str(archive)], check=True)
    work = base / 'mooncake-transfer-engine/tent/src'
    command = shlex.split((work / 'CMakeFiles/tent_shared.dir/link.txt').read_text())
    command[command.index('-o') + 1] = str(out / 'libtent_shared.so')
    command = [str(archive) if x == 'transport/rdma/libtent_xport_rdma.a' else x for x in command]
    command[1:1] = [str(out / (n + '.cpp.o')) for n in ('observer', 'osc_trace', 'candidate_policy')]
    subprocess.run(command, cwd=work, check=True)
    (out / 'build-manifest.json').write_text(json.dumps(dict(
        parent_manifest_sha256=sha(old / 'build-manifest.json'),
        source={n: sha(here / n) for n in ('candidate_policy.h', 'candidate_policy.cpp', 'build_candidates.py')},
        outputs={p.name: sha(p) for p in out.iterdir() if p.is_file()}, command=command), indent=2))
    print('CANDIDATES_BUILD_OK', out, flush=True)


if __name__ == '__main__':
    main()
