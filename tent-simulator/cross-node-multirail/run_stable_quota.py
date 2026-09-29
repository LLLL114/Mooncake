#!/usr/bin/env python3
"""Run fresh SQ pilots and three-arm steady/saturated comparisons sequentially."""
import argparse
import fcntl
import hashlib
from pathlib import Path
import random
import subprocess
import run_oscillation as old
import run_suite_collection_v2 as suite
from run_followups_bc import validate as validate_arrivals
from stable_quota_metrics import audit_trace

BASE, HERE = old.BASE, Path(__file__).resolve().parent
ROOT = BASE / 'stable-quota-20260929'
POLICIES = ('reference', 'stable', 'bounded')


def cases_for(stage):
    ref_path = BASE / 'rate-batch-20260929/reference.json'
    capacities = suite._read(ref_path)['service_bytes_per_second']
    reference = dict(bytes_per_second=capacities['D0'], source=str(ref_path),
        source_sha256=hashlib.sha256(ref_path.read_bytes()).hexdigest(),
        scope='same-day earlier RB effective capacity; fixed for this entire comparison')
    cases = []
    for repeat in range(1, 2 if stage == 'pilot' else 4):
        variants = [(p, rps) for p in range(3) for rps in ((675,) if stage == 'pilot' else (0,) if stage == 'saturated' else (225, 675))]
        random.Random(202609299 + repeat).shuffle(variants)
        for policy, rps in variants:
            args = argparse.Namespace(peer='10.0.1.251', latency_window_ms=1000,
                seconds=8 if stage == 'pilot' else 30 if stage == 'saturated' else 60,
                warmup=1 if stage == 'pilot' else 5, output_root=ROOT,
                tent_library=str(BASE / ('build-oscillation-20260917' if not policy else 'build-stable-quota-20260929') / 'libtent_shared.so'),
                stream_library=str(BASE / 'build-v2/stream_native.so'))
            sender = HERE / ('oscillation_sender.py' if not policy else 'stable_quota_sender.py')
            context = suite._context(args)
            context.update(sender=str(sender), sender_sha256=hashlib.sha256(sender.read_bytes()).hexdigest(),
                head=subprocess.check_output(['git','-C',str(HERE.parents[1]),'rev-parse','HEAD'],text=True).strip(),
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
            load = 'saturated' if not rps else '20pct' if rps == 225 else '60pct'
            case = suite._case(args, context, 'SQ-'+stage, 'D0', 1048576, 128, repeat, load,
                rate=rps * 1048576, reference=reference)
            case.update(variant=POLICIES[policy], stable_policy=policy, protocol='SQ-20260929-v1',
                has_trace=True, requests_per_second=rps, capacities=[capacities['S0'],capacities['S1']])
            p = case['parameters']
            p.update(alpha=.01, arrival_alignment='staggered', label=f'SQ-{stage}-{POLICIES[policy]}-{load}-r{repeat}')
            if not policy:
                p.update(fixed_equal=False, symmetric_prior=False)
            else:
                p.update(stable_policy=policy, rail0_bps=capacities['S0'], rail1_bps=capacities['S1'])
            cases.append(case)
    return cases


def validate(case, attempt):
    path = Path(attempt['runPath'])
    suite.read_goodput(path, case)
    if case['parameters']['rate']:
        validate_arrivals(case, attempt)
    obs, m = suite._read(path/'observer.json'), suite._read(path/'manifest.json')
    # This frozen receiver omits the field when the default is used.
    # Its exact module and unmodified default configuration are audited separately.
    receiver = m['receiver']
    default_receiver = receiver.get('module_sha256') == '1029698b8287b5fe2d7b28d9e3fd63c3febae5990800813153488cd71c62979d'
    if (obs['incomplete'] or receiver.get('rdma_num_lanes', 6) != 6
            or ('rdma_num_lanes' not in receiver and not default_receiver)):
        raise ValueError('incomplete observer or wrong receiver lanes')
    for thread in obs['threads']:
        for row in thread['rows']:
            if row['kind'] == 'release_sample' and any(row['values'][1][k] != .01 for k in ('min','max')):
                raise ValueError('original background EWMA alpha changed')
    result = audit_trace(path, case['stable_policy'], case['capacities'])
    suite._save(path/'SQ-audit.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['pilot','formal','saturated'], required=True)
    args = parser.parse_args()
    ROOT.mkdir(exist_ok=True)
    with (ROOT/'.suite.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.stage != 'pilot':
            previous = 'pilot' if args.stage == 'formal' else 'formal'
            if suite._read(ROOT/('progress-'+previous+'.json')).get('complete') is not True:
                raise ValueError('previous stage incomplete')
        path = ROOT/('plan-'+args.stage+'.json')
        if path.exists():
            raise ValueError('existing plan; inspect rather than overwrite')
        cases = cases_for(args.stage)
        suite._save(path, dict(stage=args.stage,cases=cases),exclusive=True)
        progress = dict(stage=args.stage,planned=len(cases),complete=False,outcomes=[])
        target = ROOT/('progress-'+args.stage+'.json')
        for case in cases:
            old.peer_check()
            attempt = suite.run_case(case, ROOT)
            item = dict(case_hash=suite.case_hash(case),run_path=attempt.get('runPath'),status=attempt['status'])
            progress['outcomes'].append(item); suite._save(target,progress)
            if attempt['status'] != 'success':
                raise ValueError('failed attempt retained; no automatic retry')
            try:
                item['audit'] = validate(case,attempt)
            except Exception as error:
                item['audit_error'] = str(error); suite._save(target,progress); raise
            suite._save(target,progress)
            print('SQ_PROGRESS',args.stage,len(progress['outcomes']),len(cases),flush=True)
        progress['complete'] = True; suite._save(target,progress)
        print('SQ_COMPLETE',args.stage,flush=True)


if __name__ == '__main__':
    main()
