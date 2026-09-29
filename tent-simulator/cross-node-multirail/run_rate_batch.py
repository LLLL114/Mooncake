#!/usr/bin/env python3
"""Sequential RB pilots, instrumentation controls, and the 2x2 factorial."""
import argparse
import fcntl
import hashlib
from pathlib import Path
import random
import subprocess
import run_oscillation as old
import run_suite_collection_v2 as suite
from run_followups_bc import validate as validate_arrivals
from rate_batch_metrics import audit_trace

BASE, HERE = old.BASE, Path(__file__).resolve().parent
ROOT = BASE / 'rate-batch-20260929'
POLICIES = ('original', 'legacy-remainder', 'fixed-remainder', 'aggregate-remainder', 'fixed-batch', 'aggregate-batch')


def cases_for(stage):
    reference_path = ROOT / 'reference.json'
    ref = suite._read(reference_path)
    c = ref['service_bytes_per_second']
    reference = dict(bytes_per_second=c['D0'], source=str(reference_path),
        source_sha256=hashlib.sha256(reference_path.read_bytes()).hexdigest(),
        scope='same-day D0 short saturated reference, not guaranteed physical peak')
    cases = []
    for repeat in range(1, 2 if stage == 'pilot' else 4):
        if stage == 'overhead':
            variants = [(-1, 0.), (0, 0.)]
            if repeat % 2 == 0:
                variants.reverse()
        else:
            variants = [(i, f) for i in (range(6) if stage == 'pilot' else range(-1,6)) for f in ((.6,) if stage == 'pilot' else (.2, .6))]
            random.Random(202609295 + repeat).shuffle(variants)
        for policy, fraction in variants:
            original = policy == -1
            variant = 'reference' if original else POLICIES[policy]
            args = argparse.Namespace(peer='10.0.1.251', latency_window_ms=1000,
                seconds=8 if stage == 'pilot' else 15 if stage == 'overhead' else 60,
                warmup=1 if stage == 'pilot' else 3 if stage == 'overhead' else 5,
                output_root=ROOT,
                tent_library=str(BASE / ('build-oscillation-20260917' if original else 'build-rate-batch-20260929') / 'libtent_shared.so'),
                stream_library=str(BASE / 'build-v2/stream_native.so'))
            sender = HERE / ('oscillation_sender.py' if original else 'rate_batch_sender.py')
            context = suite._context(args)
            context.update(sender=str(sender), sender_sha256=hashlib.sha256(sender.read_bytes()).hexdigest(),
                head=subprocess.check_output(['git','-C',str(HERE.parents[1]),'rev-parse','HEAD'],text=True).strip(),
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                validator_sha256=hashlib.sha256((HERE / 'rate_batch_metrics.py').read_bytes()).hexdigest())
            profile = 'saturated' if not fraction else f'{round(fraction * 100)}pct'
            request_rate = round(fraction * c['D0'] / 1048576) if fraction else 0
            offered_rate = request_rate * 1048576
            case = suite._case(args, context, 'RB-' + stage, 'D0', 1048576, 128, repeat, profile,
                rate=offered_rate, reference=reference)
            case.update(variant=variant, protocol='RB-20260929-v2-integer-arrivals', has_trace=True,
                nominal_fraction=fraction, actual_fraction=offered_rate/c['D0'], requests_per_second=request_rate)
            p = case['parameters']
            p.update(alpha=.01, arrival_alignment='staggered', label=f'RB-{stage}-{variant}-{profile}-r{repeat}')
            if original:
                p.update(fixed_equal=False, symmetric_prior=False)
            else:
                p.update(rate_batch_policy=policy, rail0_bps=c['S0'], rail1_bps=c['S1'])
            cases.append(case)
    return cases


def validate(case, attempt):
    path = Path(attempt['runPath'])
    suite.read_goodput(path, case)
    if case['parameters']['rate']:
        validate_arrivals(case, attempt)
    observer, manifest = suite._read(path / 'observer.json'), suite._read(path / 'manifest.json')
    if observer['incomplete'] or manifest['receiver'].get('rdma_num_lanes') == 1:
        raise ValueError('invalid observation or receiver lanes')
    names = manifest['environment']['nic_id_to_name']
    bounds = {name: set() for name in names.values()}
    for t in observer['threads']:
        for r in t['rows']:
            if r['kind'] == 'release_sample' and any(r['values'][1][k] != .01 for k in ('min','max')):
                raise ValueError('background legacy EWMA alpha differs')
            if r['kind'] == 'release_bounds':
                for key in ('min','max'):
                    bounds[names[str(r['dev'])]].add(tuple(round(v[key] * 8e-9, 6) for v in r['values']))
    if bounds != {'erdma_0': {(100.,10.,1000.)}, 'erdma_1': {(10.,1.,100.)}}:
        raise ValueError('background legacy bounds changed: ' + str(bounds))
    if case['variant'] == 'reference':
        from oscillation_metrics import audit_trace as original_audit
        result, _ = original_audit(path, False, False)
        suite._save(path / 'RB-audit.json', result)
        return result
    p = case['parameters']
    result, _ = audit_trace(path, p['rate_batch_policy'], [p['rail0_bps'],p['rail1_bps']])
    suite._save(path / 'RB-audit.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['pilot','overhead','formal'], required=True)
    args = parser.parse_args()
    with (ROOT / '.suite.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = 'calibration' if args.stage == 'pilot' else 'pilot' if args.stage == 'overhead' else 'overhead'
        if suite._read(ROOT / ('progress-' + previous + '.json')).get('complete') is not True:
            raise ValueError('previous stage incomplete')
        if args.stage == 'formal' and suite._read(ROOT / 'overhead-analysis.json')['stop_for_overhead']:
            raise ValueError('instrumentation throughput gate failed')
        path = ROOT / ('plan-' + args.stage + '.json')
        if path.exists():
            raise ValueError('existing plan: inspect before a documented resume')
        cases = cases_for(args.stage)
        suite._save(path, dict(stage=args.stage, cases=cases), exclusive=True)
        progress = dict(stage=args.stage, planned=len(cases), complete=False, outcomes=[])
        target = ROOT / ('progress-' + args.stage + '.json')
        for case in cases:
            old.peer_check()
            attempt = suite.run_case(case, ROOT)
            item = dict(case_hash=suite.case_hash(case), run_path=attempt.get('runPath'), status=attempt['status'])
            progress['outcomes'].append(item); suite._save(target, progress)
            if attempt['status'] != 'success':
                raise ValueError('failure retained; no silent retry')
            try:
                item['audit'] = validate(case, attempt)
            except Exception as error:
                item['audit_error'] = str(error); suite._save(target, progress)
                raise
            suite._save(target, progress)
            print('RB_PROGRESS', args.stage, len(progress['outcomes']), len(cases), flush=True)
        progress['complete'] = True; suite._save(target, progress)
        print('RB_STAGE_COMPLETE', args.stage, flush=True)


if __name__ == '__main__':
    main()
