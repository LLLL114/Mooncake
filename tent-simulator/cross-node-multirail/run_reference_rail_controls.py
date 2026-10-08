#!/usr/bin/env python3
"""Reference-only Rail controls after the provider observation gate failed."""
import fcntl
import hashlib
from pathlib import Path
import run_suite_collection_v2 as suite
from run_oscillation import BASE,peer_check
from run_provider_diagnostic import cases_for,validate,hardware_counters

ROOT=BASE/'reference-rail-controls-20261008'


def main():
    ROOT.mkdir(exist_ok=True)
    with (ROOT/'.suite.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (ROOT/'plan.json').exists(): raise ValueError('existing plan retained; no silent retry')
        cases=[]
        for c in cases_for('formal'):
            if c['diagnostic']: continue
            c['phase']='RC-formal';c['protocol']='RC-20261008-v1'
            c['parameters'].update(seconds=60.,warmup=5.,latency_window_ms=3000,
                label=c['parameters']['label'].replace('PV-formal','RC-formal'),output_root=str(ROOT/'runs'))
            c['context'].update(latency_window_ms=3000,runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
            cases.append(c)
        suite._save(ROOT/'plan.json',dict(cases=cases),exclusive=True)
        progress=dict(planned=len(cases),complete=False,outcomes=[])
        for c in cases:
            peer_check();counter=ROOT/'counters'/suite.case_hash(c)
            suite._save(counter/'before.json',hardware_counters(),exclusive=True)
            attempt=suite.run_case(c,ROOT)
            suite._save(counter/'after.json',hardware_counters(),exclusive=True)
            item=dict(case_hash=suite.case_hash(c),run_path=attempt.get('runPath'),status=attempt['status'])
            progress['outcomes'].append(item);suite._save(ROOT/'progress.json',progress)
            if attempt['status']!='success': raise ValueError('failed run retained')
            try: item['audit']=validate(c,attempt)
            except Exception as error:
                item['audit_error']=str(error);suite._save(ROOT/'progress.json',progress);raise
            suite._save(ROOT/'progress.json',progress)
            print('RC_PROGRESS',len(progress['outcomes']),len(cases),flush=True)
        progress['complete']=True;suite._save(ROOT/'progress.json',progress)
        print('RC_COMPLETE',flush=True)


if __name__=='__main__': main()
