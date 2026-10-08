#!/usr/bin/env python3
"""Private overlay: prove real verbs entry, preserve quota guard and routing."""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from build_oscillation import BASE, REPO, once, sha

OUT = BASE/'build-provider-diagnostic-20261008'


def main():
    parent=BASE/'build-poll-diagnostic-20260930'
    here=Path(__file__).resolve().parent
    manifest=json.loads((parent/'build-manifest.json').read_text())
    names=('workers.cpp','observer.h','observer.cpp','osc_trace.h','osc_trace.cpp',
           'poll_diagnostic.h','poll_diagnostic.cpp','libtent_xport_rdma.a')
    for name in names:
        if sha(parent/name)!=manifest['outputs'][name]: raise ValueError('parent changed: '+name)
    OUT.mkdir() # Never overwrite a measured or failed build.
    for name in names: shutil.copy2(parent/name,OUT/name)
    for name in ('provider_diagnostic.h','provider_diagnostic.cpp'): shutil.copy2(here/name,OUT/name)
    worker=(OUT/'workers.cpp').read_text()
    worker='#include "provider_diagnostic.h"\n'+worker
    worker=once(worker,'        int nr_poll = cq->poll(kPollCount, wc);',
        '        ::tent_provider::entered = false;\n        int nr_poll = cq->poll(kPollCount, wc);')
    worker=once(worker,'::tent_pd::poll(tl_wid, index, pd_begin, poll_ts, nr_poll)',
        '::tent_provider::poll(tl_wid, index, pd_begin, poll_ts, nr_poll)')
    worker=once(worker,'slice->source_addr, slice->length, slice->enqueue_ts, slice->submit_ts);',
        'slice->source_addr, slice->length, slice->enqueue_ts, slice->submit_ts, wc[i].qp_num);')
    (OUT/'workers.cpp').write_text(worker)
    header=(OUT/'poll_diagnostic.h').read_text()
    header=once(header,'uint64_t submit) noexcept;', 'uint64_t submit, uint32_t qpn=0) noexcept;')
    (OUT/'poll_diagnostic.h').write_text(header)
    source='#include "provider_diagnostic.h"\n'+(OUT/'poll_diagnostic.cpp').read_text()
    source=once(source,'uint64_t bytes,uint64_t enqueue,uint64_t submit) noexcept {',
        'uint64_t bytes,uint64_t enqueue,uint64_t submit,uint32_t qpn) noexcept {')
    source=once(source,'uint32_t(w),uint32_t(dev),uint32_t(qp),0};',
        'uint32_t(w),uint32_t(dev),uint32_t(qp),qpn};')
    source=once(source,'tent-poll-diagnostic-v1','tent-provider-diagnostic-v1')
    source=once(source,'        const std::string root(directory);',
        '        ::tent_provider::dump(directory);\n        const std::string root(directory);')
    (OUT/'poll_diagnostic.cpp').write_text(source)
    cq_path=REPO/'mooncake-transfer-engine/tent/src/transport/rdma/cq.cpp'
    cq=cq_path.read_text()
    if 'if (cqe_now_.load(std::memory_order_relaxed) == 0) return 0;' not in cq:
        raise ValueError('CQ quota guard differs')
    cq=once(cq,'    int rc = ibv_poll_cq(cq_, num_entries, wc);',
        '    if (::tent_pd::active) ::tent_provider::entered = true;\n'
        '    int rc = ibv_poll_cq(cq_, num_entries, wc);')
    (OUT/'cq.cpp').write_text('#include "provider_diagnostic.h"\n#include "poll_diagnostic.h"\n'+cq)
    base=REPO/'build-tent'
    flags=base/'mooncake-transfer-engine/tent/src/transport/rdma/CMakeFiles/tent_xport_rdma.dir/flags.make'
    values=dict(line.split(' = ',1) for line in flags.read_text().splitlines() if ' = ' in line)
    options=sum((shlex.split(values[k]) for k in ('CXX_DEFINES','CXX_INCLUDES','CXX_FLAGS')),[])+['-I'+str(OUT)]
    for name in ('workers','cq','observer','osc_trace','poll_diagnostic','provider_diagnostic'):
        subprocess.run(['/usr/bin/c++',*options,'-c',str(OUT/(name+'.cpp')),'-o',str(OUT/(name+'.cpp.o'))],check=True)
    archive=OUT/'libtent_xport_rdma.a'
    subprocess.run(['ar','r',str(archive),str(OUT/'workers.cpp.o'),str(OUT/'cq.cpp.o')],check=True)
    subprocess.run(['ranlib',str(archive)],check=True)
    work=base/'mooncake-transfer-engine/tent/src'
    command=shlex.split((work/'CMakeFiles/tent_shared.dir/link.txt').read_text())
    command[command.index('-o')+1]=str(OUT/'libtent_shared.so')
    command=[str(archive) if x=='transport/rdma/libtent_xport_rdma.a' else x for x in command]
    command[1:1]=[str(OUT/(n+'.cpp.o')) for n in ('observer','osc_trace','poll_diagnostic','provider_diagnostic')]
    subprocess.run(command,cwd=work,check=True)
    (OUT/'build-manifest.json').write_text(json.dumps(dict(parent_sha256=sha(parent/'build-manifest.json'),
        cq_input_sha256=sha(cq_path),source={n:sha(here/n) for n in ('build_provider_diagnostic.py','provider_diagnostic.h','provider_diagnostic.cpp')},
        outputs={p.name:sha(p) for p in OUT.iterdir() if p.is_file()},command=command),indent=2))
    print('PROVIDER_BUILD_OK',flush=True)


if __name__=='__main__': main()
