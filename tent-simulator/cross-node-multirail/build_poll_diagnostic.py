#!/usr/bin/env python3
"""Private O workers overlay: timing only, no allocation or transport-policy change."""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from build_oscillation import BASE,REPO,once,sha

OUT=BASE/'build-poll-diagnostic-20260930'


def main():
    old,v2,out,here=BASE/'build-oscillation-20260917',BASE/'build-v2',OUT,Path(__file__).resolve().parent
    if out.exists(): raise ValueError('fresh output required')
    m=json.loads((old/'build-manifest.json').read_text())
    for name in ('quota.cpp','observer.h','observer.cpp','osc_trace.h','osc_trace.cpp','libtent_xport_rdma.a'):
        if sha(old/name)!=m['outputs'][name]: raise ValueError('frozen O changed: '+name)
    vm=json.loads((v2/'source/observer-manifest.json').read_text())
    if sha(v2/'source/workers.cpp')!=vm['files']['workers.cpp']['output_sha256']: raise ValueError('worker changed')
    out.mkdir()
    for name in ('observer.h','observer.cpp','osc_trace.h','osc_trace.cpp'):
        shutil.copy2(old/name,out/name)
    for name in ('poll_diagnostic.h','poll_diagnostic.cpp'):
        shutil.copy2(here/name,out/name)
    worker='#include "poll_diagnostic.h"\n#include <cerrno>\n'+(v2/'source/workers.cpp').read_text()
    worker=once(worker,'    int num_slices = 0;\n\n    uint64_t current_ts = getCurrentTimeInNano();',
        '    int num_slices = 0;\n\n    uint64_t current_ts = getCurrentTimeInNano();\n'
        '    if (::tent_pd::active) ::tent_pd::phase(tl_wid, current_ts);')
    worker=once(worker,'            worker.inflight_slices.load(std::memory_order_relaxed);\n        if (inflight_slices ||',
        '            worker.inflight_slices.load(std::memory_order_relaxed);\n'
        '        if (::tent_pd::active) ::tent_pd::cycle(tl_wid, current_ts, inflight_slices);\n        if (inflight_slices ||')
    worker=once(worker,'        int nr_poll = cq->poll(kPollCount, wc);\n        if (nr_poll < 0) continue;\n        auto poll_ts = getCurrentTimeInNano();',
        '        const auto pd_begin = ::tent_pd::active ? getCurrentTimeInNano() : 0;\n'
        '        int nr_poll = cq->poll(kPollCount, wc);\n'
        '        if (nr_poll < 0) { if (::tent_pd::active) ::tent_pd::reject(EIO); continue; }\n'
        '        auto poll_ts = getCurrentTimeInNano();\n'
        '        if (::tent_pd::active) ::tent_pd::poll(tl_wid, index, pd_begin, poll_ts, nr_poll);')
    worker=once(worker,'            releaseSliceQuota(device_selector_.get(), slice, sample_lat_sec);',
        '            int pd_token = -1;\n'
        '            if (::tent_pd::active) {\n'
        '                if (ewma_sample && slice->retry_count == 0)\n'
        '                    pd_token = ::tent_pd::complete(tl_wid, index, slice->qp_index,\n'
        '                        slice->task->request.source, slice->task->request.length,\n'
        '                        slice->source_addr, slice->length, slice->enqueue_ts, slice->submit_ts);\n'
        '                else ::tent_pd::reject(EIO);\n'
        '            }\n'
        '            releaseSliceQuota(device_selector_.get(), slice, sample_lat_sec);')
    worker=once(worker,'                    worker.perf.enqueue_lat.add(enqueue_lat);\n                }',
        '                    worker.perf.enqueue_lat.add(enqueue_lat);\n                }\n'
        '                if (pd_token >= 0) ::tent_pd::handled(tl_wid, pd_token, getCurrentTimeInNano());')
    (out/'workers.cpp').write_text(worker)
    base=REPO/'build-tent'; flags=base/'mooncake-transfer-engine/tent/src/transport/rdma/CMakeFiles/tent_xport_rdma.dir/flags.make'
    values=dict(line.split(' = ',1) for line in flags.read_text().splitlines() if ' = ' in line)
    options=sum((shlex.split(values[k]) for k in ('CXX_DEFINES','CXX_INCLUDES','CXX_FLAGS')),[])+['-I'+str(out)]
    for name in ('workers','observer','osc_trace','poll_diagnostic'):
        subprocess.run(['/usr/bin/c++',*options,'-c',str(out/(name+'.cpp')),'-o',str(out/(name+'.cpp.o'))],check=True)
    archive=out/'libtent_xport_rdma.a';shutil.copy2(old/archive.name,archive)
    subprocess.run(['ar','r',str(archive),str(out/'workers.cpp.o')],check=True)
    subprocess.run(['ranlib',str(archive)],check=True)
    work=base/'mooncake-transfer-engine/tent/src'
    command=shlex.split((work/'CMakeFiles/tent_shared.dir/link.txt').read_text())
    command[command.index('-o')+1]=str(out/'libtent_shared.so')
    command=[str(archive) if x=='transport/rdma/libtent_xport_rdma.a' else x for x in command]
    command[1:1]=[str(out/(n+'.cpp.o')) for n in ('observer','osc_trace','poll_diagnostic')]
    subprocess.run(command,cwd=work,check=True)
    (out/'build-manifest.json').write_text(json.dumps(dict(parent_sha256=sha(old/'build-manifest.json'),
        worker_input_sha256=sha(v2/'source/workers.cpp'),source={n:sha(here/n) for n in ('poll_diagnostic.h','poll_diagnostic.cpp','build_poll_diagnostic.py')},
        outputs={p.name:sha(p) for p in out.iterdir() if p.is_file()},command=command),indent=2))
    print('POLL_DIAGNOSTIC_BUILD_OK',flush=True)


if __name__=='__main__': main()
