#!/usr/bin/env python3
"""SSH-only same-day single-rail service references, with original V2 library."""
import argparse
import fcntl
from pathlib import Path
import random
import statistics

import run_suite_collection_v2 as suite
from run_oscillation import peer_check

BASE = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')
ROOT = BASE / 'rate-batch-20260929'


def main():
    ROOT.mkdir(exist_ok=True)
    args = argparse.Namespace(peer='10.0.1.251', latency_window_ms=1000, seconds=12,
        warmup=3, output_root=ROOT,
        tent_library=str(BASE / 'build-v2/libtent_shared.so'),
        stream_library=str(BASE / 'build-v2/stream_native.so'))
    context = suite._context(args)
    with (ROOT / '.suite.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan_path = ROOT / 'plan-calibration.json'
        if plan_path.exists():
            raise ValueError('calibration plan exists; inspect before any resume')
        cases = []
        for repeat in (1, 2, 3):
            modes = ['S0', 'S1', 'D0']; random.Random(202609290 + repeat).shuffle(modes)
            for mode in modes:
                c = suite._case(args, context, 'RB-calibration', mode, 1048576, 128,
                    repeat, 'saturated', rate=0)
                c['parameters'].update(alpha=.01, label=f'RB-calibration-{mode}-r{repeat}')
                cases.append(c)
        suite._save(plan_path, dict(cases=cases), exclusive=True)
        progress = dict(complete=False, planned=len(cases), outcomes=[])
        rates = {mode: [] for mode in ('S0', 'S1', 'D0')}
        for c in cases:
            peer_check()
            attempt = suite.run_case(c, ROOT)
            item = dict(mode=c['mode'], repeat=c['repeat'], case_hash=suite.case_hash(c),
                        status=attempt['status'], run_path=attempt.get('runPath'))
            progress['outcomes'].append(item)
            suite._save(ROOT / 'progress-calibration.json', progress)
            if attempt['status'] != 'success':
                raise ValueError('calibration failure retained')
            g = suite.read_goodput(Path(attempt['runPath']), c)
            item['goodput_gbps'] = g * 8e-9
            rates[c['mode']].append(g)
            suite._save(ROOT / 'progress-calibration.json', progress)
            print('RB_CALIBRATION', len(progress['outcomes']), c['mode'], g * 8e-9, flush=True)
        progress['complete'] = True
        suite._save(ROOT / 'progress-calibration.json', progress)
        reference = dict(scope='same-day single-rail effective service reference, not physical capacity',
            repetitions_bytes_per_second=rates,
            service_bytes_per_second={m: statistics.median(v) for m, v in rates.items()})
        suite._save(ROOT / 'reference.json', reference, exclusive=True)
        print('RB_CALIBRATION_COMPLETE', reference, flush=True)


if __name__ == '__main__':
    main()
