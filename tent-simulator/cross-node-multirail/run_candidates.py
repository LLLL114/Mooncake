#!/usr/bin/env python3
"""Serial same-input candidate matrix; no automatic retries or data overwrite."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import random
import subprocess

import run_oscillation as original
from candidate_metrics import audit_trace
from run_followups_bc import validate as validate_arrivals
import run_suite_collection_v2 as suite

BASE = original.BASE
ROOT = BASE / 'candidates-20260917'
HERE = Path(__file__).resolve().parent
POLICIES = ('original', 'smooth', 'hysteresis', 'remainder')


def cases_for(stage):
    source = BASE / 'E-20260917/reference.json'
    ref = suite._read(source)
    ref = dict(d0_bytes_per_second=ref['d0_bytes_per_second'], source=str(source),
               source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
               scope='same O reference input, not physical capacity')
    cases = []
    for repeat in range(1, 2 if stage == 'pilot' else 4):
        variants = [(k, f) for k in range(4) for f in ((.6,) if stage == 'pilot' else (.2, .6))]
        random.Random(202609175 + repeat).shuffle(variants)
        for kind, fraction in variants:
            args = argparse.Namespace(peer='10.0.1.251', latency_window_ms=1000,
                seconds=8 if stage == 'pilot' else 60, warmup=1 if stage == 'pilot' else 5,
                output_root=ROOT, tent_library=str(BASE / 'build-candidates-20260917/libtent_shared.so'),
                stream_library=str(BASE / 'build-v2/stream_native.so'))
            context = suite._context(args)
            context.update(sender=str(HERE / 'candidate_sender.py'),
                sender_sha256=hashlib.sha256((HERE / 'candidate_sender.py').read_bytes()).hexdigest(),
                head=subprocess.check_output(['git', '-C', str(HERE.parents[1]), 'rev-parse', 'HEAD'], text=True).strip(),
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                validator_sha256=hashlib.sha256((HERE / 'candidate_metrics.py').read_bytes()).hexdigest())
            case = suite._case(args, context, 'CAND-' + stage, 'D0', 1048576, 128, repeat,
                f'{round(fraction * 100)}pct', rate=fraction * ref['d0_bytes_per_second'], reference=ref)
            case.update(variant=POLICIES[kind], protocol='CAND-20260917-v1', has_trace=True)
            case['parameters'].update(alpha=.01, arrival_alignment='staggered', candidate_policy=kind,
                label=f'CAND-{stage}-{POLICIES[kind]}-{round(fraction * 100)}pct-r{repeat}')
            cases.append(case)
    return cases


def validate(case, attempt):
    path = Path(attempt['runPath'])
    suite.read_goodput(path, case)
    validate_arrivals(case, attempt)
    observer, manifest = suite._read(path / 'observer.json'), suite._read(path / 'manifest.json')
    if observer['incomplete'] or manifest['receiver'].get('rdma_num_lanes') == 1:
        raise ValueError('invalid observation or receiver lanes')
    names = manifest['environment']['nic_id_to_name']
    bounds = {name: set() for name in names.values()}
    for t in observer['threads']:
        for r in t['rows']:
            if r['kind'] == 'release_sample' and any(r['values'][1][k] != .01 for k in ('min', 'max')):
                raise ValueError('unexpected bandwidth alpha')
            if r['kind'] == 'release_bounds':
                for key in ('min', 'max'):
                    bounds[names[str(r['dev'])]].add(tuple(round(v[key] * 8e-9, 6) for v in r['values']))
    if bounds != {'erdma_0': {(100., 10., 1000.)}, 'erdma_1': {(10., 1., 100.)}}:
        raise ValueError('unexpected bandwidth bounds: ' + str(bounds))
    audit, _ = audit_trace(path, case['parameters']['candidate_policy'])
    suite._save(path / 'CAND-audit.json', audit)
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['pilot', 'formal'], required=True)
    args = parser.parse_args()
    ROOT.mkdir(exist_ok=True)
    with (ROOT / '.suite.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.stage == 'formal' and suite._read(ROOT / 'progress-pilot.json').get('complete') is not True:
            raise ValueError('pilot is incomplete')
        path = ROOT / ('plan-' + args.stage + '.json')
        if path.exists():
            raise ValueError('existing plan: inspect results, do not silently repeat')
        cases = cases_for(args.stage)
        suite._save(path, dict(stage=args.stage, cases=cases), exclusive=True)
        progress = dict(stage=args.stage, planned=len(cases), complete=False, outcomes=[])
        target = ROOT / ('progress-' + args.stage + '.json')
        for case in cases:
            original.peer_check()
            attempt = suite.run_case(case, ROOT)
            item = dict(case_hash=suite.case_hash(case), run_path=attempt.get('runPath'), status=attempt['status'])
            progress['outcomes'].append(item); suite._save(target, progress)
            if attempt['status'] != 'success':
                raise ValueError('failed attempt preserved')
            try:
                item['audit'] = validate(case, attempt)
            except Exception as error:
                item['audit_error'] = str(error); suite._save(target, progress)
                raise
            suite._save(target, progress)
            print('CAND_PROGRESS', args.stage, len(progress['outcomes']), len(cases), flush=True)
        progress['complete'] = True; suite._save(target, progress)
        print('CAND_STAGE_COMPLETE', args.stage, flush=True)


if __name__ == '__main__':
    main()
