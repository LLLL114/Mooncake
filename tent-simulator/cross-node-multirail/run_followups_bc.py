#!/usr/bin/env python3
"""SSH-only B/C experiments using the frozen full TENT sender and capacity."""
import argparse
import fcntl
import hashlib
import json
import math
from pathlib import Path
import random
import sys

import run_suite_collection_v2 as baseline

BASE = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')


def cases_for(args, capacity):
    entry = capacity['sizes']['1048576']
    rate, q = entry['reference_bytes_per_second'], entry['q']
    context = baseline._context(args)
    reference = dict(capacity_sha256=hashlib.sha256(args.capacity.read_bytes()).hexdigest(),
                     d0_bytes_per_second=rate, saturation_confirmed=entry['saturation_confirmed'])
    cases = []
    for repeat in range(1, (1 if args.pilot else args.repetitions) + 1):
        variants = []
        if args.phase == 'B':
            for profile in (['pilot-step'] if args.pilot else ['step', 'pulse']):
                if profile == 'pilot-step':
                    phases = [(2, .2), (4, .4), (2, .2)]
                elif profile == 'step':
                    phases = [(20, .2), (20, .8), (20, .2)]
                else:
                    phases = [(20, .2)] + [(s, f) for _ in range(20) for s, f in ((.1, 1.2), (1.9, .2))] + [(20, .2)]
                steps = [dict(seconds=s, rate_bytes_per_second=f * rate) for s, f in phases]
                variants.append((profile, .2, 1, 'staggered', None, steps))
        else:
            for fraction in (.6, .9):
                for callers, alignment in ((1, 'staggered'), (4, 'synchronized'), (4, 'staggered'),
                                           (8, 'synchronized'), (8, 'staggered')):
                    variants.append((f'{int(fraction*100)}pct-c{callers}-{alignment}', fraction, callers, alignment, None, None))
                # num_lanes changes QP/CQ resources too: a separate resource control.
                variants.append((f'{int(fraction*100)}pct-lanes1', fraction, 1, 'staggered', 1, None))
            if args.pilot:
                variants = [variants[1]]
        random.Random(20260916 + repeat).shuffle(variants)
        for label, fraction, callers, alignment, lanes, steps in variants:
            args.seconds = sum(s['seconds'] for s in steps) if steps else (8 if args.pilot else 120)
            args.warmup = 1 if args.pilot else 10
            phase = args.phase + ('-pilot' if args.pilot else '')
            case = baseline._case(args, context, phase, 'D0', 1048576, q, repeat, label,
                                  rate=fraction * rate, reference=reference)
            p = case['parameters']
            p.update(callers=callers, arrival_alignment=alignment, workers=lanes,
                     steps=json.dumps(steps, separators=(',', ':')) if steps else None)
            case['protocol'] = 'BC-20260916-v1'
            case['resource_scope'] = ('num_lanes=1 changes per-NIC QP/CQ resources; not isolated worker causality'
                                      if lanes else 'fixed total Q, aggregate input and original per-NIC QP/CQ configuration')
            case['timing_scope'] = 'frozen sender warmup/drain10; no dynamic timeline or policy changes'
            cases.append(case)
    return cases


def validate(case, result):
    root = Path(result['runPath'])
    read = baseline._read
    manifest, native, observer = [read(root / (n + '.json')) for n in ('manifest', 'stream-summary', 'observer')]
    p = case['parameters']
    if observer.get('incomplete') is not False or manifest.get('data_verified') is not True:
        raise ValueError('incomplete observer or missing payload verification')
    arguments = read(root / 'arguments.json')
    if any(arguments.get(k) != v for k, v in p.items()):
        raise ValueError('actual arguments differ from frozen case')
    steps = json.loads(p['steps']) if p['steps'] else [dict(seconds=p['seconds'], rate_bytes_per_second=p['rate'])]
    if native.get('steps') != steps or native.get('arrival_alignment') != p['arrival_alignment']:
        raise ValueError('native steps/alignment differ from case')
    if native.get('window') != p['window'] or native.get('callers') != p['callers']:
        raise ValueError('native total window/caller count differs')
    bounds, elapsed = [], 0
    for step in steps:
        start = elapsed
        elapsed += round(step['seconds'] * 1e9)
        bounds.append((start, elapsed))
    if elapsed != native['measurement_end_ns'] - native['measurement_start_ns']:
        raise ValueError('native step durations differ from measurement interval')
    arrivals, flows, ids = [0] * len(steps), [0] * p['callers'], set()
    phase_counts = {'warmup': 0, 'measurement': 0}
    with (root / 'requests.jsonl').open() as stream:
        for line in stream:
            r = json.loads(line)
            if r['request_id'] in ids or r['status'] != 'success' or r['bytes'] != p['size']:
                raise ValueError('duplicate/non-successful/wrong-size request')
            ids.add(r['request_id'])
            phase_counts[r['phase']] += 1
            if r['phase'] != 'measurement':
                continue
            offset = r['planned_ns'] - native['measurement_start_ns']
            index = next((i for i, (left, right) in enumerate(bounds) if left <= offset < right), None)
            if index is None or not 0 <= r['flow_id'] < p['callers']:
                raise ValueError('request outside planned step/flow')
            arrivals[index] += 1
            flows[r['flow_id']] += 1
    if len(ids) != native['total']['accepted'] or any(phase_counts[k] != native[k]['accepted'] for k in phase_counts):
        raise ValueError('native/raw request conservation mismatch')
    expected = [math.ceil(s['seconds'] * s['rate_bytes_per_second'] / p['size']) for s in steps]
    if any(abs(a - e) > p['callers'] for a, e in zip(arrivals, expected)):
        raise ValueError('actual step arrivals do not match fixed aggregate input')
    if min(flows) <= 0 or max(flows)-min(flows) > len(steps):
        raise ValueError('measured flow counts do not match the equal-share caller budget')
    audit = dict(case_hash=baseline.case_hash(case), passed=True, steps=steps,
                 planned_arrivals_by_step=expected, accepted_arrivals_by_step=arrivals,
                 measured_requests_by_flow=flows, records=len(ids), observer_complete=True,
                 scope='protocol/accounting audit; final-slot payload verification from sender; no new transfer')
    target = root / 'followup-audit.json'
    if target.exists():
        if read(target) != audit:
            raise ValueError('saved followup audit differs from raw evidence')
    else:
        baseline._save(target, audit, exclusive=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=['B', 'C'], required=True)
    parser.add_argument('--subset', choices=['all', 'core', 'resources'], default='all')
    parser.add_argument('--pilot', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--repetitions', type=int, default=5)
    parser.add_argument('--output-root', type=Path, default=BASE / 'followups-bc')
    parser.add_argument('--capacity', type=Path, default=BASE / 'suites-extended/capacity.json')
    parser.add_argument('--peer', default='10.0.1.251')
    parser.add_argument('--tent-library', default=str(BASE / 'build-v2/libtent_shared.so'))
    parser.add_argument('--stream-library', default=str(BASE / 'build-v2/stream_native.so'))
    args = parser.parse_args()
    args.latency_window_ms = 1000 if args.phase == 'B' else 5000
    if args.repetitions < 1:
        parser.error('repetitions must be positive')
    if args.subset != 'all' and (args.phase != 'C' or args.pilot):
        parser.error('subsets are only for formal C cases')
    args.output_root = baseline._outside_repo(args.output_root)
    capacity = baseline.load_capacity(args.capacity, args)
    cases = cases_for(args, capacity)
    args.output_root.mkdir(parents=True, exist_ok=True)
    phase = args.phase + ('-pilot' if args.pilot else '')
    plan_path = args.output_root / ('plan-' + phase + '.json')
    plan = dict(protocol='BC-20260916-v1', cases=cases,
                limitation='R0 omitted: topology-first RR does not cover both NICs; phase-specific reversals not inferred from whole-run counters')
    if plan_path.exists():
        if baseline._read(plan_path) != plan:
            raise ValueError('frozen plan differs; use a separate output root')
    else:
        baseline._save(plan_path, plan, exclusive=True)
    if args.subset != 'all':
        cases = [c for c in cases if (c['parameters']['workers'] is None) == (args.subset == 'core')]
    print('PLAN', phase, args.subset, len(cases), str(plan_path), flush=True)
    if args.dry_run:
        return 0
    with (args.output_root / '.suite.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        progress = dict(phase=phase, subset=args.subset, complete=False, planned=len(cases), outcomes=[])
        progress_name = phase if args.subset == 'all' else phase + '-' + args.subset
        progress_path = args.output_root / ('progress-' + progress_name + '.json')
        baseline._save(progress_path, progress)
        for case in cases:
            result = baseline.run_case(case, args.output_root)
            item = dict(case_hash=baseline.case_hash(case), run_path=result.get('runPath'), status=result['status'])
            progress['outcomes'].append(item)
            try:
                if result['status'] != 'success':
                    raise ValueError(result.get('error', 'native sender failed'))
                validate(case, result)
                item['protocol_audit'] = 'passed'
            except (OSError, ValueError, KeyError, TypeError) as error:
                item.update(protocol_audit='failed', error=str(error))
                baseline._save(progress_path, progress)
                print('FOLLOWUP_STOPPED', item, flush=True)
                return 1
            baseline._save(progress_path, progress)
        progress['complete'] = True
        baseline._save(progress_path, progress)
        print('PHASE_COMPLETE', phase, len(cases), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
