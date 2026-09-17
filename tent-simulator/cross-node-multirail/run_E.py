#!/usr/bin/env python3
"""Serial, resumable E stages. Run on A10 via run_E.sh; never on the Mac."""
import argparse
import fcntl
import hashlib
import json
import math
from pathlib import Path
import random
import socket
import statistics
import subprocess

import run_suite_collection_v2 as suite

BASE = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')
ROOT = BASE / 'E-20260917'
HERE = Path(__file__).resolve().parent
VARIANTS = {'original': (.01, None), 'control': (.01, 1), 'k10': (.01, 10),
            'k100': (.01, 100), 'a050': (.5, 1), 'a090': (.9, 1), 'a099': (.99, 1)}


def peer_check():
    with socket.create_connection(('10.0.1.251', 19930), timeout=15) as conn:
        conn.settimeout(15)
        with conn.makefile('rwb') as f:
            f.write(b'{"op":"hello"}\n'); f.flush()
            reply = json.loads(f.readline(1048576))
    e = reply.get('environment', {})
    if (reply.get('ok') is not True or reply.get('slots') != 128 or e.get('nic') != 'erdma_0'
            or e.get('rdma_num_lanes') == 1 or e.get('module_sha256') !=
            '1029698b8287b5fe2d7b28d9e3fd63c3febae5990800813153488cd71c62979d'):
        raise ValueError('receiver contract mismatch')
    return e


def reference():
    p = ROOT / 'reference.json'
    if p.exists():
        return suite._read(p)
    source = BASE / 'restore-default-20260916/progress.json'
    progress = suite._read(source)
    if progress.get('complete') is not True or len(progress['outcomes']) != 3:
        raise ValueError('default restoration incomplete')
    rates = []
    for result in progress['outcomes']:
        if result['status'] != 'success':
            raise ValueError('restoration failed')
        rates.append(suite.read_goodput(result['runPath']))
    value = dict(d0_bytes_per_second=statistics.median(rates), samples=rates,
                 source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                 saturation_confirmed=False, scope='three 10-second Q128 D0 controls; not physical peak')
    suite._save(p, value, exclusive=True)
    return value


def make_cases(stage, ref):
    cases = []
    if stage == 'dynamic':
        selected = suite._read(ROOT / 'steady-analysis.json')['selected']
    else:
        selected = []
    for repeat in range(1, 6):
        if stage == 'overhead':
            variants = [('original', 'saturated'), ('control', 'saturated')]
            if repeat % 2 == 0:
                variants.reverse()
        elif stage == 'steady':
            variants = [(v, load) for v in VARIANTS if v != 'original' for load in ('90pct', 'saturated')]
            random.Random(20260917 + repeat).shuffle(variants)
        else:
            variants = [(v, profile) for v in ['control', *selected] for profile in ('step', 'pulse')]
            random.Random(20261917 + repeat).shuffle(variants)
        for variant, profile in variants:
            alpha, stride = VARIANTS[variant]
            steps = None
            rate = 0 if profile == 'saturated' else ref['d0_bytes_per_second'] * .9
            seconds = 60
            if stage == 'dynamic':
                phases = [(20, .2), (20, .8), (20, .2)] if profile == 'step' else (
                    [(20, .2)] + [(s, f) for _ in range(20) for s, f in ((.1, 1.2), (1.9, .2))] + [(20, .2)])
                steps = [dict(seconds=s, rate_bytes_per_second=f * ref['d0_bytes_per_second']) for s, f in phases]
                rate, seconds = steps[0]['rate_bytes_per_second'], sum(s for s, _ in phases)
            args = argparse.Namespace(peer='10.0.1.251', latency_window_ms=1000,
                seconds=seconds, warmup=5, output_root=ROOT,
                tent_library=str(BASE / ('build-v2' if variant == 'original' else 'build-e-20260917') / 'libtent_shared.so'),
                stream_library=str(BASE / 'build-v2/stream_native.so'))
            context = suite._context(args)
            sender = HERE / ('native_sender_extended.py' if variant == 'original' else 'learning_sender.py')
            context.update(sender=str(sender), sender_sha256=hashlib.sha256(sender.read_bytes()).hexdigest(),
                repository_head=subprocess.check_output(['git', '-C', str(HERE.parents[1]), 'rev-parse', 'HEAD'], text=True).strip(),
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                underlying_sender_sha256=hashlib.sha256((HERE / 'native_sender_extended.py').read_bytes()).hexdigest())
            case = suite._case(args, context, 'E-' + stage, 'D0', 1048576, 128, repeat, profile, rate=rate, reference=ref)
            case.update(variant=variant, protocol='E-20260917-v2')
            case['parameters'].update(alpha=alpha, arrival_alignment='staggered',
                label=f'E-{stage}-{variant}-{profile}-r{repeat}',
                steps=json.dumps(steps, separators=(',', ':')) if steps else None)
            if stride is not None:
                case['parameters']['learning_stride'] = stride
            cases.append(case)
    return cases


def validate(case, attempt):
    root = Path(attempt['runPath'])
    p = case['parameters']
    suite.read_goodput(root, case)
    actual = suite._read(root / 'arguments.json')
    if any(actual.get(k) != v for k, v in p.items()):
        raise ValueError('actual arguments differ from case')
    m, o, config = (suite._read(root / (n + '.json')) for n in ('manifest', 'observer', 'tent-config'))
    if o.get('incomplete') is not False or config['transports']['rdma']['bandwidth_learning_rate'] != p['alpha']:
        raise ValueError('observer incomplete or alpha differs')
    if m['receiver'].get('rdma_num_lanes') == 1:
        raise ValueError('receiver unexpectedly has one lane')
    from collections import defaultdict
    gates = defaultdict(lambda: [0, 0])
    for thread_index, thread in enumerate(o['threads']):
        for row in thread['rows']:
            if row['kind'] == 'release_sample' and any(row['values'][1][k] != p['alpha'] for k in ('min', 'max')):
                raise ValueError('actual observed EWMA alpha differs')
            if row['kind'] in ('release_bw', 'release_no_learning'):
                if row['kind'] == 'release_no_learning' and row['values'][0]['min'] <= 0:
                    raise ValueError('non-positive latency prevents exact eligible-gate audit')
                gates[(thread_index, row['dev'])][row['kind'] == 'release_no_learning'] += row['n']
    stride = p.get('learning_stride', 1)
    if not gates or any(abs(update - (update + skip) / stride) > 1.000001 for update, skip in gates.values()):
        raise ValueError('actual update/skip counts disagree with per-thread/NIC stride')
    if p['rate'] > 0:
        from run_followups_bc import validate as validate_arrivals
        validate_arrivals(case, attempt)
    else:
        from collections import Counter
        counts, ids = Counter(), set()
        with (root / 'requests.jsonl').open() as f:
            for line in f:
                r = json.loads(line)
                if r['request_id'] in ids or r['status'] != 'success' or r['bytes'] != p['size']:
                    raise ValueError('invalid closed-loop request accounting')
                ids.add(r['request_id']); counts[r['phase']] += 1
        if len(ids) != m['total']['accepted'] or any(counts[k] != m[k]['accepted'] for k in ('warmup', 'measurement')):
            raise ValueError('closed-loop raw/manifest accounting mismatch')
    audit = dict(passed=True, stride=stride, alpha=p['alpha'],
        updates=sum(v[0] for v in gates.values()), skipped=sum(v[1] for v in gates.values()),
        contexts=len(gates), scope='observed measurement interval; TLS/NIC edge error <=1 update')
    suite._save(root / 'E-audit.json', audit)
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['overhead', 'steady', 'dynamic'], required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / '.suite.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.stage == 'steady' and suite._read(ROOT / 'overhead-analysis.json')['stop_for_overhead']:
            raise ValueError('experimental control overhead requires investigation')
        cases = make_cases(args.stage, reference())
        plan = dict(stage=args.stage, protocol='E-20260917-v2', cases=cases)
        path = ROOT / ('plan-' + args.stage + '.json')
        if path.exists() and suite._read(path) != plan:
            raise ValueError('existing frozen E plan differs')
        if not path.exists():
            suite._save(path, plan, exclusive=True)
        print('E_PLAN', args.stage, len(cases), flush=True)
        if args.dry_run:
            return
        progress = dict(stage=args.stage, planned=len(cases), complete=False, outcomes=[])
        target = ROOT / ('progress-' + args.stage + '.json')
        for case in cases:
            peer_check()
            result = suite.run_case(case, ROOT)
            item = dict(case_hash=suite.case_hash(case), status=result['status'], run_path=result.get('runPath'))
            progress['outcomes'].append(item)
            suite._save(target, progress)
            if result['status'] != 'success':
                raise ValueError('failed E attempt retained; investigate before continuing')
            try:
                item['audit'] = validate(case, result)
            except Exception as error:
                item['audit_error'] = str(error); suite._save(target, progress)
                raise
            suite._save(target, progress)
        progress['complete'] = True
        suite._save(target, progress)
        print('E_STAGE_COMPLETE', args.stage, len(cases), flush=True)


if __name__ == '__main__':
    main()
