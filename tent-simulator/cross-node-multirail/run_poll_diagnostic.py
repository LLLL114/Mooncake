#!/usr/bin/env python3
"""Controlled timing observation; no new routing policy in this experiment."""
import argparse
import fcntl
import hashlib
from pathlib import Path
import random
import subprocess
import run_suite_collection_v2 as suite
from run_oscillation import BASE,peer_check
from run_stable_quota import validate as validate_original
from poll_diagnostic_metrics import audit

ROOT=BASE/'poll-diagnostic-20260930'
HERE=Path(__file__).resolve().parent


def cases_for(stage):
    cases=[]
    for repeat in range(1,2 if stage=='pilot' else 4):
        variants=[(timed,rps) for timed in (False,True) for rps in ((225,) if stage=='pilot' else (0,) if stage=='overhead' else (225,675))]
        random.Random(202609301+repeat).shuffle(variants)
        for timed,rps in variants:
            name='timed' if timed else 'reference';load='saturated' if not rps else '225rps' if rps==225 else '675rps'
            args=argparse.Namespace(peer='10.0.1.251',latency_window_ms=1000,
                seconds=8 if stage=='pilot' else 15 if stage=='overhead' else 30,
                warmup=1 if stage=='pilot' else 3 if stage=='overhead' else 5,output_root=ROOT,
                tent_library=str(BASE/('build-poll-diagnostic-20260930' if timed else 'build-oscillation-20260917')/'libtent_shared.so'),
                stream_library=str(BASE/'build-v2/stream_native.so'))
            sender=HERE/('poll_diagnostic_sender.py' if timed else 'oscillation_sender.py')
            context=suite._context(args)
            context.update(sender=str(sender),sender_sha256=hashlib.sha256(sender.read_bytes()).hexdigest(),
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                head=subprocess.check_output(['git','-C',str(HERE.parents[1]),'rev-parse','HEAD'],text=True).strip())
            c=suite._case(args,context,'PD-'+stage,'D0',1048576,128,repeat,load,rate=rps*1048576)
            c.update(variant=name,diagnostic=timed,protocol='PD-20260930-v1',has_trace=True,stable_policy=0,capacities=[])
            c['parameters'].update(alpha=.01,arrival_alignment='staggered',label=f'PD-{stage}-{name}-{load}-r{repeat}')
            if timed: c['parameters']['poll_diagnostic']=True
            else: c['parameters'].update(fixed_equal=False,symmetric_prior=False)
            cases.append(c)
    return cases


def validate(case,attempt):
    result=validate_original(case,attempt)
    if case['diagnostic']: result['poll_diagnostic']=audit(Path(attempt['runPath']))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',choices=['pilot','overhead','formal'],required=True);args=p.parse_args()
    with (ROOT/'.suite.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.stage!='pilot':
            prior='pilot' if args.stage=='overhead' else 'overhead'
            if not suite._read(ROOT/('progress-'+prior+'.json'))['complete']: raise ValueError('prior stage incomplete')
        if args.stage=='formal' and not suite._read(ROOT/'overhead-analysis.json')['gate_pass']: raise ValueError('observation overhead gate failed')
        cases=cases_for(args.stage); plan=ROOT/('plan-'+args.stage+'.json')
        if plan.exists(): raise ValueError('existing plan: do not overwrite/retry silently')
        suite._save(plan,dict(cases=cases),exclusive=True)
        progress=dict(stage=args.stage,planned=len(cases),complete=False,outcomes=[]); target=ROOT/('progress-'+args.stage+'.json')
        for case in cases:
            peer_check();attempt=suite.run_case(case,ROOT)
            item=dict(case_hash=suite.case_hash(case),run_path=attempt.get('runPath'),status=attempt['status'])
            progress['outcomes'].append(item);suite._save(target,progress)
            if attempt['status']!='success': raise ValueError('failed run retained')
            try: item['audit']=validate(case,attempt)
            except Exception as error:
                item['audit_error']=str(error);suite._save(target,progress);raise
            suite._save(target,progress);print('PD_PROGRESS',args.stage,len(progress['outcomes']),len(cases),flush=True)
        progress['complete']=True;suite._save(target,progress);print('PD_STAGE_COMPLETE',args.stage,flush=True)


if __name__=='__main__': main()
