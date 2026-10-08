#!/usr/bin/env python3
"""Frozen, serial real-provider observation and matched-load Rail controls."""
import argparse
import fcntl
import hashlib
from pathlib import Path
import random
import subprocess
import run_suite_collection_v2 as suite
from run_oscillation import BASE, peer_check
from run_followups_bc import validate as validate_arrivals
from run_stable_quota import validate as validate_original
from poll_diagnostic_metrics import audit, load

ROOT=BASE/'provider-diagnostic-20261008'
HERE=Path(__file__).resolve().parent


def cases_for(stage):
    cases=[]
    for repeat in range(1,2 if stage=='pilot' else 4):
        variants=[(mode,timed,rps) for mode in (('D0','S0','S1') if stage!='overhead' else ('D0',))
                  for timed in (False,True) for rps in ((225,) if stage=='pilot' else (0,) if stage=='overhead' else (225,675))]
        random.Random(202610080+repeat).shuffle(variants)
        for mode,timed,rps in variants:
            name='provider' if timed else 'reference'; profile=f'{rps}rps' if rps else 'saturated'
            args=argparse.Namespace(peer='10.0.1.251',latency_window_ms=1000,
                seconds=8 if stage=='pilot' else 15 if stage=='overhead' else 30,
                warmup=1 if stage=='pilot' else 3 if stage=='overhead' else 5,output_root=ROOT,
                tent_library=str(BASE/('build-provider-diagnostic-20261008' if timed else 'build-oscillation-20260917')/'libtent_shared.so'),
                stream_library=str(BASE/'build-v2/stream_native.so'))
            sender=HERE/('poll_diagnostic_sender.py' if timed else 'oscillation_sender.py')
            context=suite._context(args)
            context.update(sender=str(sender),sender_sha256=hashlib.sha256(sender.read_bytes()).hexdigest(),
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                head=subprocess.check_output(['git','-C',str(HERE.parents[1]),'rev-parse','HEAD'],text=True).strip())
            c=suite._case(args,context,'PV-'+stage,mode,1048576,128,repeat,profile,rate=rps*1048576)
            c.update(variant=name,diagnostic=timed,protocol='PV-20261008-v1',has_trace=mode=='D0',stable_policy=0,capacities=[])
            c['parameters'].update(alpha=.01,arrival_alignment='staggered',label=f'PV-{stage}-{mode}-{name}-{profile}-r{repeat}')
            if timed: c['parameters']['poll_diagnostic']=True
            else: c['parameters'].update(fixed_equal=False,symmetric_prior=False)
            cases.append(c)
    return cases


def validate(case,attempt):
    root=Path(attempt['runPath'])
    if case['mode']=='D0': result=validate_original(case,attempt)
    else:
        suite.read_goodput(root,case);validate_arrivals(case,attempt)
        obs=suite._read(root/'observer.json'); m=suite._read(root/'manifest.json')
        if obs['incomplete'] or m['receiver'].get('rdma_num_lanes',6)!=6:
            raise ValueError('invalid observer/receiver')
        result=dict(single_rail=True)
    if case['diagnostic']:
        result['diagnostic']=audit(root,single_rail=case['mode']!='D0')
        meta,records,_=load(root)
        provider=suite._read(root/'provider-summary.json')
        by_key={(w['worker'],p['dev']):p for w in meta['workers'] for p in w['polls']}
        completions=0
        for row in provider['rows']:
            key=(row['worker'],row['dev']);completions+=row['completions']
            if row['outer']!=row['backend']+row['skipped'] or row['empty']+row['full']>row['backend']:
                raise ValueError('provider counter conservation failed')
            if row['backend']!=by_key.get(key,{}).get('calls',0): raise ValueError('provider timing count mismatch')
        if completions!=sum(len(s) for s in records.values()): raise ValueError('provider WC coverage differs')
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',choices=['pilot','overhead','formal'],required=True);args=p.parse_args()
    ROOT.mkdir(exist_ok=True)
    with (ROOT/'.suite.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.stage!='pilot':
            previous='pilot' if args.stage=='overhead' else 'overhead'
            if not suite._read(ROOT/('progress-'+previous+'.json'))['complete']: raise ValueError('previous stage incomplete')
        if args.stage=='formal' and not suite._read(ROOT/'overhead-analysis.json')['gate_pass']:
            raise ValueError('observation overhead gate failed; formal stopped')
        plan=ROOT/('plan-'+args.stage+'.json')
        if plan.exists(): raise ValueError('existing plan: inspect instead of silent retry')
        cases=cases_for(args.stage);suite._save(plan,dict(cases=cases),exclusive=True)
        progress=dict(stage=args.stage,planned=len(cases),complete=False,outcomes=[])
        target=ROOT/('progress-'+args.stage+'.json')
        for case in cases:
            peer_check()
            counter_path=ROOT/'counters'/suite.case_hash(case)
            suite._save(counter_path/'before.json',hardware_counters(),exclusive=True)
            attempt=suite.run_case(case,ROOT)
            suite._save(counter_path/'after.json',hardware_counters(),exclusive=True)
            item=dict(case_hash=suite.case_hash(case),run_path=attempt.get('runPath'),status=attempt['status'])
            progress['outcomes'].append(item);suite._save(target,progress)
            if attempt['status']!='success': raise ValueError('failed run retained')
            try: item['audit']=validate(case,attempt)
            except Exception as error:
                item['audit_error']=str(error);suite._save(target,progress);raise
            suite._save(target,progress);print('PV_PROGRESS',args.stage,len(progress['outcomes']),len(cases),flush=True)
        progress['complete']=True;suite._save(target,progress);print('PV_STAGE_COMPLETE',args.stage,flush=True)


def hardware_counters():
    result={}
    for path in Path('/sys/class/infiniband').glob('*/ports/1/*counters/*'):
        if not path.is_file(): continue
        try: result[str(path)]=int(path.read_text().strip())
        except (OSError,ValueError): result[str(path)]=None
    return result


if __name__=='__main__': main()
