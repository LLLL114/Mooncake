#!/usr/bin/env python3
"""Relink a private TENT library from existing matching build objects."""
import argparse,hashlib,json,shlex,shutil,subprocess
from pathlib import Path


def run(command, cwd=None):
    print('BUILD',shlex.join(command),flush=True)
    subprocess.run(command,cwd=cwd,check=True)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--repo',default='/root/mooncake')
    p.add_argument('--existing-build',default='/root/mooncake/build-tent')
    p.add_argument('--output',default='/root/mooncake-tent-multirdma-output/cross-node-multirail/build-v2')
    a=p.parse_args();repo=Path(a.repo).resolve();base=Path(a.existing_build).resolve();out=Path(a.output).resolve();here=Path(__file__).resolve().parent
    if repo==out or repo in out.parents:raise ValueError('build output must be outside repository')
    if (out/'libtent_shared.so').exists():raise ValueError('choose a fresh output directory; do not overwrite a possibly loaded test library')
    for name in ('libtent_shared.so','stream_native.so'):
        target=out/name
        if target.exists() or target.is_symlink():
            raise ValueError(f'refusing to overwrite existing shared library: {target}; use a new build directory')
    out.mkdir(parents=True,exist_ok=True)
    flags=base/'mooncake-transfer-engine/tent/src/transport/rdma/CMakeFiles/tent_xport_rdma.dir/flags.make'
    values={l.split(' = ',1)[0]:l.split(' = ',1)[1] for l in flags.read_text().splitlines() if ' = ' in l}
    options=sum((shlex.split(values[k]) for k in ['CXX_DEFINES','CXX_INCLUDES','CXX_FLAGS']),[])+['-I'+str(here)]
    # The observer generator makes shadow source copies and validates anchors.
    run(['python',str(here/'instrument_tent.py'),'--source-root',str(repo),'--builddir',str(out/'source')])
    objs=[]
    for name in ['quota','workers','rdma_transport']:
        src=out/'source'/(name+'.cpp');obj=out/(name+'.cpp.o')
        run(['/usr/bin/c++',*options,'-c',str(src),'-o',str(obj)]);objs.append(obj)
    obs=out/'observer.cpp.o'
    run(['/usr/bin/c++',*options,'-c',str(here/'observer.cpp'),'-o',str(obs)])
    original=base/'mooncake-transfer-engine/tent/src/transport/rdma/libtent_xport_rdma.a'
    archive=out/'libtent_xport_rdma.a';shutil.copy2(original,archive)
    run(['ar','r',str(archive),*[str(p) for p in objs]]);run(['ranlib',str(archive)])
    workdir=base/'mooncake-transfer-engine/tent/src'
    command=shlex.split((workdir/'CMakeFiles/tent_shared.dir/link.txt').read_text())
    command[command.index('-o')+1]=str(out/'libtent_shared.so')
    command=[str(archive) if s=='transport/rdma/libtent_xport_rdma.a' else s for s in command]
    command.insert(1,str(obs))
    run(command,cwd=workdir)
    stream=here/'stream_native.cpp'
    if stream.exists():run(['/usr/bin/c++','-std=c++20','-O3','-fPIC','-shared','-pthread','-I'+str(repo/'mooncake-transfer-engine/tent/include'),str(stream),'-o',str(out/'stream_native.so')])
    introspection=here/'native_introspection.cpp'
    if introspection.exists():run(['/usr/bin/c++',*options,'-shared',str(introspection),'-o',str(out/'native_introspection.so')])
    def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
    manifest={'repo':str(repo),'head':subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),'existing_build':str(base),'original_library':str(workdir/'libtent_shared.so'),'original_library_sha256':sha(workdir/'libtent_shared.so'),'observer_library':str(out/'libtent_shared.so'),'observer_library_sha256':sha(out/'libtent_shared.so'),'flags':values,'link_command':command,'production_sources':{n:sha(repo/'mooncake-transfer-engine/tent/src/transport/rdma'/f'{n}.cpp') for n in ['quota','workers','rdma_transport']},'test_sources':{p.name:sha(p) for p in here.iterdir() if p.suffix in ['.cpp','.h','.py']}}
    (out/'build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print('BUILD_OBSERVED_OK',out,flush=True)

if __name__=='__main__':main()
