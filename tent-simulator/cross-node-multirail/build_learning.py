#!/usr/bin/env python3
"""SSH-only E library: one learning gate, no fault/endpoint/worker hooks."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    repo = Path('/root/mooncake')
    base = repo / 'build-tent'
    observed = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail/build-v2')
    here = Path(__file__).resolve().parent
    out = args.output.resolve()
    if out.exists() or out == repo or repo in out.parents:
        raise ValueError('output must be a new directory outside the repository')
    if sha(observed / 'libtent_shared.so') != '858e3afaa3cbef4773eeafc9ce8e3e8db1701c02e71fe5b6ac17a575e9528aee':
        raise ValueError('observed V2 library differs')
    manifest = json.loads((observed / 'source/observer-manifest.json').read_text())
    quota = observed / 'source/quota.cpp'
    if sha(quota) != manifest['files']['quota.cpp']['output_sha256']:
        raise ValueError('observed quota source differs')
    production = repo / 'mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp'
    if sha(production) != manifest['files']['quota.cpp']['source_sha256']:
        raise ValueError('production quota source differs')
    old = '    if (!smart_selection_enabled_ || latency <= 0.0) {'
    text = quota.read_text()
    if text.count(old) != 1:
        raise ValueError('learning gate anchor mismatch')
    text = '#include "learning_stride.h"\n' + text.replace(
        old, '    if (!smart_selection_enabled_ || latency <= 0.0 || !tent_e_should_learn(dev_id)) {')
    out.mkdir(parents=True)
    (out / 'quota.cpp').write_text(text)
    for name in ('learning_stride.cpp', 'learning_stride.h'):
        shutil.copy2(here / name, out / name)
    flags = base / 'mooncake-transfer-engine/tent/src/transport/rdma/CMakeFiles/tent_xport_rdma.dir/flags.make'
    values = dict(line.split(' = ', 1) for line in flags.read_text().splitlines() if ' = ' in line)
    options = sum((shlex.split(values[k]) for k in ('CXX_DEFINES', 'CXX_INCLUDES', 'CXX_FLAGS')), [])
    options += ['-I' + str(out), '-I' + str(observed / 'source')]

    def run(command, cwd=None):
        subprocess.run(command, cwd=cwd, check=True)

    for name in ('quota', 'learning_stride'):
        run(['/usr/bin/c++', *options, '-c', str(out / (name + '.cpp')), '-o', str(out / (name + '.cpp.o'))])
    archive = out / 'libtent_xport_rdma.a'
    shutil.copy2(observed / 'libtent_xport_rdma.a', archive)
    run(['ar', 'r', str(archive), str(out / 'quota.cpp.o')])
    run(['ranlib', str(archive)])
    work = base / 'mooncake-transfer-engine/tent/src'
    command = shlex.split((work / 'CMakeFiles/tent_shared.dir/link.txt').read_text())
    command[command.index('-o') + 1] = str(out / 'libtent_shared.so')
    command = [str(archive) if part == 'transport/rdma/libtent_xport_rdma.a' else part for part in command]
    command[1:1] = [str(observed / 'observer.cpp.o'), str(out / 'learning_stride.cpp.o')]
    run(command, cwd=work)
    provenance = dict(scope='E learning gate only; no fault injection',
        head=subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip(),
        input_sha256={str(f): sha(f) for f in [quota, production, observed / 'libtent_shared.so',
            observed / 'libtent_xport_rdma.a', observed / 'observer.cpp.o', flags,
            here / 'learning_stride.cpp', here / 'learning_stride.h', here / 'build_learning.py']},
        output_sha256={f.name: sha(f) for f in out.iterdir() if f.is_file()}, link_command=command)
    (out / 'build-manifest.json').write_text(json.dumps(provenance, indent=2) + '\n')
    print('LEARNING_BUILD_COMPLETE', out, flush=True)


if __name__ == '__main__':
    main()
