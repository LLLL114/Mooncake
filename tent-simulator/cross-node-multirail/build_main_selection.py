#!/usr/bin/env python3
"""Build decision-only observation against the freshly merged main build."""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from build_oscillation import BASE,REPO,once,sha

ROOT=BASE/'main-baseline-20261008'


def main():
    here=Path(__file__).resolve().parent;base=ROOT/'build';out=ROOT/'observed'
    if subprocess.check_output(['git','-C',str(REPO),'diff','origin/main','HEAD','--','mooncake-transfer-engine/tent'],text=True):
        raise ValueError('production TENT differs from fetched main')
    out.mkdir()
    for n in ('main_selection_trace.h','main_selection_trace.cpp','osc_trace.h'):shutil.copy2(here/n,out/n)
    source=REPO/'mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp'
    text='#include "main_selection_trace.h"\n'+source.read_text()
    text=once(text,'    slice_dev_ids.clear();',
        '    ::main_trace::Allocation trace(this,total_length,num_slices,slice_bytes,\n'
        '        sched_params_.score_epsilon,slice_dev_ids);\n    slice_dev_ids.clear();')
    text=once(text,'        candidates.push_back(c);',
        '        candidates.push_back(c);\n        ::main_trace::candidate(dev_id,score,inflight,ewma_bw,rank_penalty);')
    text=once(text,'        bool probe_mode = ((++tl_call_count % 100) == 0);',
        '        bool probe_mode = ((++tl_call_count % 100) == 0);\n        ::main_trace::probe(probe_mode);')
    text=once(text,'            uint32_t assigned =',
        '            ::main_trace::weight(candidates[i].dev_id,w,total_weight);\n            uint32_t assigned =')
    text=once(text,'    return Status::OK();\n}\n\nvoid DeviceSelector::auditStrictLocalNuma()',
        '    ::main_trace::success();\n    return Status::OK();\n}\n\nvoid DeviceSelector::auditStrictLocalNuma()')
    (out/'quota.cpp').write_text(text)
    transport=REPO/'mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp'
    body=once(transport.read_text(),'    for (auto& request : request_list) {\n',
        '    for (auto& request : request_list) {\n'
        '        ::main_trace::RequestScope trace_request(request.source,request.length);\n')
    (out/'rdma_transport.cpp').write_text('#include "main_selection_trace.h"\n'+body)
    f=base/'mooncake-transfer-engine/tent/src/transport/rdma/CMakeFiles/tent_xport_rdma.dir/flags.make'
    values=dict(l.split(' = ',1) for l in f.read_text().splitlines() if ' = ' in l)
    options=sum((shlex.split(values[k]) for k in ('CXX_DEFINES','CXX_INCLUDES','CXX_FLAGS')),[])+['-I'+str(out)]
    for n in ('quota','rdma_transport','main_selection_trace'):
        subprocess.run(['/usr/bin/c++',*options,'-c',str(out/(n+'.cpp')),'-o',str(out/(n+'.cpp.o'))],check=True)
    archive=out/'libtent_xport_rdma.a'
    shutil.copy2(base/'mooncake-transfer-engine/tent/src/transport/rdma/libtent_xport_rdma.a',archive)
    subprocess.run(['ar','r',str(archive),str(out/'quota.cpp.o'),str(out/'rdma_transport.cpp.o')],check=True);subprocess.run(['ranlib',str(archive)],check=True)
    work=base/'mooncake-transfer-engine/tent/src'
    command=shlex.split((work/'CMakeFiles/tent_shared.dir/link.txt').read_text())
    command[command.index('-o')+1]=str(out/'libtent_shared.so')
    command=[str(archive) if x=='transport/rdma/libtent_xport_rdma.a' else x for x in command]
    command.insert(1,str(out/'main_selection_trace.cpp.o'));subprocess.run(command,cwd=work,check=True)
    driver=ROOT/'driver';driver.mkdir()
    subprocess.run(['/usr/bin/c++','-std=c++20','-O3','-fPIC','-shared','-pthread',
        '-I'+str(REPO/'mooncake-transfer-engine/tent/include'),str(here/'stream_native.cpp'),'-o',str(driver/'stream_native.so')],check=True)
    subprocess.run(['/usr/bin/c++',*options,'-shared',str(here/'native_introspection.cpp'),'-o',str(driver/'native_introspection.so')],check=True)
    (out/'build-manifest.json').write_text(json.dumps(dict(
        head=subprocess.check_output(['git','-C',str(REPO),'rev-parse','HEAD'],text=True).strip(),
        main=(ROOT/'main-target.txt').read_text().strip(),source_quota_sha256=sha(source),
        source_transport_sha256=sha(transport),
        stock_library_sha256=sha(work/'libtent_shared.so'),
        source={n:sha(here/n) for n in ('main_selection_trace.h','main_selection_trace.cpp','build_main_selection.py','osc_trace.h','stream_native.cpp','native_introspection.cpp')},
        outputs={p.name:sha(p) for p in out.iterdir() if p.is_file()},driver={p.name:sha(p) for p in driver.iterdir()},link=command),indent=2))
    print('MAIN_SELECTION_BUILD_OK',flush=True)


if __name__=='__main__':main()
