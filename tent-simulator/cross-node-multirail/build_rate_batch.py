#!/usr/bin/env python3
"""SSH-only RB shadow build: capture successful completions, preserve transport."""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from build_oscillation import BASE, REPO, once, sha

OUT = BASE / 'build-rate-batch-20260929'


def main():
    old, v2, out = BASE / 'build-oscillation-20260917', BASE / 'build-v2', OUT
    if out.exists():
        raise ValueError('fresh build output required')
    m = json.loads((old / 'build-manifest.json').read_text())
    for name in ('quota.cpp', 'observer.cpp', 'observer.h', 'osc_trace.h', 'osc_trace.cpp', 'libtent_xport_rdma.a'):
        if sha(old / name) != m['outputs'][name]:
            raise ValueError('frozen O input changed: ' + name)
    wm = json.loads((v2 / 'source/observer-manifest.json').read_text())
    if sha(v2 / 'source/workers.cpp') != wm['files']['workers.cpp']['output_sha256']:
        raise ValueError('frozen worker source changed')
    for name in ('quota.cpp', 'workers.cpp'):
        if sha(REPO / 'mooncake-transfer-engine/tent/src/transport/rdma' / name) != wm['files'][name]['source_sha256']:
            raise ValueError('production source changed: ' + name)
    here = Path(__file__).resolve().parent
    out.mkdir()
    for name in ('rate_batch_policy.h', 'rate_batch_policy.cpp'):
        shutil.copy2(here / name, out / name)
    quota = '#include "rate_batch_policy.h"\n' + (old / 'quota.cpp').read_text()
    quota = once(quota, '        info.dev_id = dev_id;',
        '        info.dev_id = dev_id;\n        ::tent_rb::bind(dev_id, local_topology_->getNicName(dev_id).c_str());')
    quota = once(quota, '        double ewma_bw = dev.getEwmaBandwidth();',
        '        double ewma_bw = tent_rb_policy() >= 2 ? ::tent_rb::capacity(dev_id, ::tent_obs::decision_stamp()) : dev.getEwmaBandwidth();')
    quota = once(quota, '        double rank_penalty = sched_params_.numa_tier_weights[rank];',
        '        double rank_penalty = tent_rb_policy() >= 2 ? 1.0 : sched_params_.numa_tier_weights[rank];')
    quota = once(quota, '        candidates.push_back(c);',
        '        candidates.push_back(c);\n        ::tent_rb::capture(dev_id, inflight, ewma_bw);')
    new = '''    if (tent_rb_policy() && !probe_mode && candidates.size() == 2 &&
        num_slices && total_length % num_slices == 0) {
        const size_t lo = candidates[0].dev_id < candidates[1].dev_id ? 0 : 1;
        const auto& a = candidates[lo]; const auto& b = candidates[lo ^ 1];
        const double ia = 1.0 / (a.score + sched_params_.score_epsilon);
        const double ib = 1.0 / (b.score + sched_params_.score_epsilon);
        double w0 = ia / (ia + ib), w1 = ib / (ia + ib);
        uint32_t counts[2];
        if (tent_rb_policy() >= 4) {
            const auto qa = ::tent_rb::input(a.dev_id), qb = ::tent_rb::input(b.dev_id);
            const auto plan = ::tent_rb::batch(qa.inflight, qb.inflight, qa.capacity, qb.capacity,
                                               num_slices, slice_bytes);
            w0 = plan.target; w1 = 1.0 - w0;
            counts[0] = plan.first; counts[1] = plan.second;
        } else {
            ::tent_rb::remainder(w0, w1, num_slices, counts[0], counts[1]);
        }
        TENT_OBS_ALLOC(::tent_obs::weight(a.dev_id, w0, 1.0));
        TENT_OBS_ALLOC(::tent_obs::weight(b.dev_id, w1, 1.0));
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
    header = (old / 'observer.h').read_text() + '\nnamespace tent_obs { uint64_t decision_stamp() noexcept; }\n'
    (out / 'observer.h').write_text(header)
    observer = '#include "rate_batch_policy.h"\n' + (old / 'observer.cpp').read_text()
    observer = once(observer, 'void weight(int dev, double inverse_score, double total_weight) noexcept {',
        'uint64_t decision_stamp() noexcept { auto* p = pending(); return p ? p->stamp : clock_ns(); }\n'
        'void weight(int dev, double inverse_score, double total_weight) noexcept {')
    observer = once(observer, '(tent_osc_fixed() ? "shadow_inverse_score" : "consumed_inverse_score")',
        '(tent_rb_policy() >= 4 ? "consumed_batch_target" : "consumed_inverse_score")')
    (out / 'observer.cpp').write_text(observer)
    worker = '#include "rate_batch_policy.h"\n' + (v2 / 'source/workers.cpp').read_text()
    worker = once(worker, '                slice->submit_ts = getCurrentTimeInNano();',
        '                slice->submit_ts = getCurrentTimeInNano();\n'
        '                ::tent_rb::posted(slice->source_dev_id, slice->length);')
    worker = once(worker, '            releaseSliceQuota(device_selector_.get(), slice, sample_lat_sec);',
        '            ::tent_rb::completed(index, slice->length,\n'
        '                slice->submit_ts, poll_ts, ewma_sample);\n'
        '            releaseSliceQuota(device_selector_.get(), slice, sample_lat_sec);')
    (out / 'workers.cpp').write_text(worker)
    shutil.copy2(old / 'osc_trace.h', out / 'osc_trace.h')
    trace = '#include "rate_batch_policy.h"\n' + (old / 'osc_trace.cpp').read_text()
    trace = trace.replace('tent-oscillation-trace-v1', 'tent-rate-batch-trace-v1')
    trace = once(trace, '<< ",\\\"capacity_per_thread\\\":" << limit',
        '<< ",\\\"rate_batch_policy\\\":" << tent_rb_policy()\n             << ",\\\"capacity_per_thread\\\":" << limit')
    (out / 'osc_trace.cpp').write_text(trace)
    base = REPO / 'build-tent'
    flags = base / 'mooncake-transfer-engine/tent/src/transport/rdma/CMakeFiles/tent_xport_rdma.dir/flags.make'
    values = dict(line.split(' = ', 1) for line in flags.read_text().splitlines() if ' = ' in line)
    options = sum((shlex.split(values[k]) for k in ('CXX_DEFINES', 'CXX_INCLUDES', 'CXX_FLAGS')), []) + ['-I' + str(out)]
    for name in ('quota', 'workers', 'observer', 'osc_trace', 'rate_batch_policy'):
        subprocess.run(['/usr/bin/c++', *options, '-c', str(out / (name + '.cpp')), '-o', str(out / (name + '.cpp.o'))], check=True)
    archive = out / 'libtent_xport_rdma.a'; shutil.copy2(old / 'libtent_xport_rdma.a', archive)
    subprocess.run(['ar', 'r', str(archive), str(out / 'quota.cpp.o'), str(out / 'workers.cpp.o')], check=True)
    subprocess.run(['ranlib', str(archive)], check=True)
    work = base / 'mooncake-transfer-engine/tent/src'
    command = shlex.split((work / 'CMakeFiles/tent_shared.dir/link.txt').read_text())
    command[command.index('-o') + 1] = str(out / 'libtent_shared.so')
    command = [str(archive) if x == 'transport/rdma/libtent_xport_rdma.a' else x for x in command]
    command[1:1] = [str(out / (n + '.cpp.o')) for n in ('observer', 'osc_trace', 'rate_batch_policy')]
    subprocess.run(command, cwd=work, check=True)
    (out / 'build-manifest.json').write_text(json.dumps(dict(parent_O_sha256=sha(old / 'build-manifest.json'),
        source={n: sha(here / n) for n in ('rate_batch_policy.h', 'rate_batch_policy.cpp', 'build_rate_batch.py')},
        worker_input_sha256=sha(v2 / 'source/workers.cpp'),
        outputs={f.name: sha(f) for f in out.iterdir() if f.is_file()}, command=command), indent=2))
    print('RATE_BATCH_BUILD_OK', out, flush=True)


if __name__ == '__main__':
    main()
