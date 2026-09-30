#!/usr/bin/env python3
"""TG cases; reuse SQ's verified sequential execution and correctness checks."""
import argparse
import hashlib
from pathlib import Path
import random
import subprocess
import run_stable_quota as runner
import run_suite_collection_v2 as suite

BASE, HERE = runner.BASE, Path(__file__).resolve().parent
ROOT = BASE / 'tail-gain-20260930'
POLICIES = ('reference', 'integer', 'gain')
validate = runner.validate


def cases_for(stage):
    ref_path = ROOT/'reference.json'
    capacities = suite._read(ref_path)['service_bytes_per_second']
    reference = dict(bytes_per_second=capacities['D0'],source=str(ref_path),
        source_sha256=hashlib.sha256(ref_path.read_bytes()).hexdigest(),
        scope='same-day TG effective path calibration; shared receiver')
    cases = []
    for repeat in range(1,2 if stage=='pilot' else 4):
        variants = [(p,rps) for p in (0,3,4) for rps in ((0,) if stage=='saturated' else (225,675))]
        random.Random(202609300+repeat).shuffle(variants)
        for policy,rps in variants:
            variant = {0:'reference',3:'integer',4:'gain'}[policy]
            args = argparse.Namespace(peer='10.0.1.251',latency_window_ms=1000,
                seconds=8 if stage=='pilot' else 30 if stage=='saturated' else 60,
                warmup=1 if stage=='pilot' else 5,output_root=ROOT,
                tent_library=str(BASE/('build-tail-gain-20260930' if policy else 'build-oscillation-20260917')/'libtent_shared.so'),
                stream_library=str(BASE/'build-v2/stream_native.so'))
            sender = HERE/('stable_quota_sender.py' if policy else 'oscillation_sender.py')
            context = suite._context(args)
            context.update(sender=str(sender),sender_sha256=hashlib.sha256(sender.read_bytes()).hexdigest(),
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                head=subprocess.check_output(['git','-C',str(HERE.parents[1]),'rev-parse','HEAD'],text=True).strip())
            load = 'saturated' if not rps else '20pct' if rps==225 else '60pct'
            c = suite._case(args,context,'TG-'+stage,'D0',1048576,128,repeat,load,
                rate=rps*1048576,reference=reference)
            c.update(variant=variant,stable_policy=policy,protocol='TG-20260930-v1',has_trace=True,
                requests_per_second=rps,capacities=[capacities['S0'],capacities['S1']])
            p = c['parameters']
            p.update(alpha=.01,arrival_alignment='staggered',label=f'TG-{stage}-{variant}-{load}-r{repeat}')
            if policy:
                p.update(stable_policy=policy,rail0_bps=capacities['S0'],rail1_bps=capacities['S1'])
            else:
                p.update(fixed_equal=False,symmetric_prior=False)
            cases.append(c)
    return cases


if __name__ == '__main__':
    if suite._read(ROOT/'progress-calibration.json').get('complete') is not True:
        raise ValueError('calibration incomplete')
    runner.ROOT, runner.cases_for = ROOT, cases_for
    runner.main()
