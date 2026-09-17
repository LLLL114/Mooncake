#!/usr/bin/env python3
"""Serial O experiments, with frozen plans and complete decision-trace checks."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import random
import socket
import subprocess

import run_suite_collection_v2 as suite
from run_followups_bc import validate as validate_arrivals
from oscillation_metrics import audit_trace

BASE = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')
ROOT = BASE / 'oscillation-20260917'
HERE = Path(__file__).resolve().parent
VARIANTS = ('native-free', 'native-equal', 'symmetric-free', 'symmetric-equal')


def peer_check():
    with socket.create_connection(('10.0.1.251', 19930), timeout=15) as conn:
        conn.settimeout(15)
        with conn.makefile('rwb') as f:
            f.write(b'{"op":"hello"}\n'); f.flush()
            reply = json.loads(f.readline(1048576))
    environment = reply.get('environment', {})
    if (reply.get('ok') is not True or reply.get('slots') != 128
            or environment.get('nic') != 'erdma_0' or environment.get('rdma_num_lanes') == 1
            or environment.get('module_sha256') != '1029698b8287b5fe2d7b28d9e3fd63c3febae5990800813153488cd71c62979d'):
        raise ValueError('receiver contract mismatch')


def reference():
    p = ROOT / 'reference.json'
    if p.exists():
        return suite._read(p)
    source = BASE / 'E-20260917/reference.json'
    old = suite._read(source)
    ref = dict(d0_bytes_per_second=old['d0_bytes_per_second'],
               source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
               scope='prior same-day short measured reference; not current capacity or physical bandwidth')
    suite._save(p, ref, exclusive=True)
    return ref


def cases_for(stage):
    ref = reference()
    result = []
    for repeat in range(1, (1 if stage == 'pilot' else 3) + 1):
        if stage == 'overhead':
            variants = [('original', .6), ('native-free', .6)]
            if repeat % 2 == 0:
                variants.reverse()
        else:
            variants = [(v, fraction) for v in VARIANTS for fraction in ((.2,) if stage == 'pilot' else (.2, .6))]
            random.Random(202609170 + repeat).shuffle(variants)
        for variant, fraction in variants:
            traced = variant != 'original'
            fixed = variant.endswith('equal')
            symmetric = variant.startswith('symmetric')
            args = argparse.Namespace(peer='10.0.1.251', latency_window_ms=1000,
                seconds=8 if stage == 'pilot' else 30 if stage == 'overhead' else 60,
                warmup=1 if stage == 'pilot' else 5, output_root=ROOT,
                tent_library=str(BASE / ('build-oscillation-20260917' if traced else 'build-v2') / 'libtent_shared.so'),
                stream_library=str(BASE / 'build-v2/stream_native.so'))
            context = suite._context(args)
            sender = HERE / ('oscillation_sender.py' if traced else 'native_sender_extended.py')
            context.update(sender=str(sender), sender_sha256=hashlib.sha256(sender.read_bytes()).hexdigest(),
                head=subprocess.check_output(['git', '-C', str(HERE.parents[1]), 'rev-parse', 'HEAD'], text=True).strip(),
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                trace_validator_sha256=hashlib.sha256((HERE / 'oscillation_metrics.py').read_bytes()).hexdigest())
            profile = f'{round(fraction * 100)}pct'
            case = suite._case(args, context, 'O-' + stage, 'D0', 1048576, 128, repeat, profile,
                               rate=fraction * ref['d0_bytes_per_second'], reference=ref)
            case.update(variant=variant, protocol='O-20260917-v1', has_trace=traced)
            case['parameters'].update(alpha=.01, arrival_alignment='staggered',
                label=f'O-{stage}-{variant}-{profile}-r{repeat}',
                config_override=json.dumps({'numa_penalties': [1, 1, 1]}, separators=(',', ':')) if symmetric else None)
            if traced:
                case['parameters'].update(fixed_equal=fixed, symmetric_prior=symmetric)
            result.append(case)
    return result


def validate(case, attempt):
    path = Path(attempt['runPath'])
    suite.read_goodput(path, case)
    validate_arrivals(case, attempt)
    p = case['parameters']
    observer = suite._read(path / 'observer.json')
    manifest = suite._read(path / 'manifest.json')
    names = manifest['environment']['nic_id_to_name']
    if observer['incomplete'] or manifest['receiver'].get('rdma_num_lanes') == 1:
        raise ValueError('invalid observation or receiver configuration')
    symmetric = p.get('symmetric_prior', False)
    bounds = {n: set() for n in names.values()}
    for thread in observer['threads']:
        for r in thread['rows']:
            if r['kind'] == 'release_sample' and any(r['values'][1][k] != .01 for k in ('min', 'max')):
                raise ValueError('alpha differs from original')
            if r['kind'] == 'release_bounds':
                for key in ('min', 'max'):
                    bounds[names[str(r['dev'])]].add(tuple(round(v[key] * 8e-9, 6) for v in r['values']))
    expected = {'erdma_0': {(10., 1., 100.)} if symmetric else {(100., 10., 1000.)},
                'erdma_1': {(10., 1., 100.)}}
    if bounds != expected:
        raise ValueError('actual bandwidth bounds differ from frozen protocol: ' + str(bounds))
    result = dict(passed=True, has_trace=case['has_trace'], fixed_equal=p.get('fixed_equal', False),
                  symmetric_prior=symmetric, request_and_input_audit=True)
    if case['has_trace']:
        trace, _ = audit_trace(path, p['fixed_equal'], symmetric)
        result['trace'] = trace
    suite._save(path / 'O-audit.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['pilot', 'overhead', 'formal'], required=True)
    parser.add_argument('--dry-run', action='store_true', help='freeze a plan but send no traffic')
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / '.suite.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.stage != 'pilot':
            previous = 'pilot' if args.stage == 'overhead' else 'overhead'
            if suite._read(ROOT / ('progress-' + previous + '.json')).get('complete') is not True:
                raise ValueError('previous validation stage is incomplete')
        cases = cases_for(args.stage)
        path = ROOT / ('plan-' + args.stage + '.json')
        plan = dict(protocol='O-20260917-v1', stage=args.stage, cases=cases)
        if path.exists() and suite._read(path) != plan:
            raise ValueError('frozen plan differs; do not overwrite or silently retry')
        if not path.exists():
            suite._save(path, plan, exclusive=True)
        print('O_PLAN', args.stage, len(cases), flush=True)
        if args.dry_run:
            return
        progress = dict(stage=args.stage, complete=False, planned=len(cases), outcomes=[])
        target = ROOT / ('progress-' + args.stage + '.json')
        for case in cases:
            peer_check()
            attempt = suite.run_case(case, ROOT)
            item = dict(case_hash=suite.case_hash(case), run_path=attempt.get('runPath'), status=attempt['status'])
            progress['outcomes'].append(item); suite._save(target, progress)
            if attempt['status'] != 'success':
                raise ValueError('failed attempt preserved; diagnose before continuing')
            try:
                item['audit'] = validate(case, attempt)
            except Exception as error:
                item['audit_error'] = str(error); suite._save(target, progress)
                raise
            suite._save(target, progress)
        progress['complete'] = True; suite._save(target, progress)
        print('O_STAGE_COMPLETE', args.stage, len(cases), flush=True)


if __name__ == '__main__':
    main()
