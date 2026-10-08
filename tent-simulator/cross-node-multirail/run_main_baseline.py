#!/usr/bin/env python3
"""Only the merged-main algorithm: stock performance and decision observation."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import random
import socket
import subprocess
import time
import run_suite_collection_v2 as suite
from build_main_selection import ROOT,REPO
from main_baseline_metrics import validate

HERE=Path(__file__).resolve().parent


def peer_check():
    with socket.create_connection(('10.0.1.251',19930),timeout=15) as s:
        f=s.makefile('rwb');f.write(b'{"op":"hello"}\n');f.flush();reply=json.loads(f.readline(1048576))
    env=reply.get('environment',{})
    if (reply.get('ok') is not True or reply.get('slots')!=128 or env.get('nic')!='erdma_0'
            or env.get('source_commit')!=(ROOT/'main-target.txt').read_text().strip()
            or env.get('rdma_num_lanes')!=6 or 'main-baseline-20261008/build/' not in env.get('module','')):
        raise ValueError('fresh receiver contract mismatch')
    path=ROOT/'receiver-contract.json'
    if path.exists():
        if suite._read(path)!=env:raise ValueError('receiver changed during collection')
    else:suite._save(path,env,exclusive=True)


def cases_for(stage):
    result=[]
    for repeat in range(1,2 if stage=='pilot' else 4):
        variants=[(traced,rps) for traced in (False,True) for rps in ((225,) if stage=='pilot' else (225,675,0))]
        random.Random(202610082+repeat).shuffle(variants)
        for traced,rps in variants:
            variant='trace' if traced else 'stock';profile=f'{rps}rps' if rps else 'saturated'
            library=ROOT/('observed/libtent_shared.so' if traced else 'build/mooncake-transfer-engine/tent/src/libtent_shared.so')
            args=argparse.Namespace(peer='10.0.1.251',latency_window_ms=3000,seconds=8 if stage=='pilot' else 60,
                warmup=1 if stage=='pilot' else 5,output_root=ROOT,tent_library=str(library),stream_library=str(ROOT/'driver/stream_native.so'))
            context=suite._context(args)
            context.update(main=(ROOT/'main-target.txt').read_text().strip(),
                head=subprocess.check_output(['git','-C',str(REPO),'rev-parse','HEAD'],text=True).strip(),
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
            c=suite._case(args,context,'MAIN-'+stage,'D0',1048576,128,repeat,profile,rate=rps*1048576)
            c.update(traced=traced,variant=variant,protocol='MAIN-20261008-v1')
            c['parameters'].update(observer_off=not traced,arrival_alignment='staggered',label=f'MAIN-{stage}-{variant}-{profile}-r{repeat}')
            result.append(c)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',choices=['pilot','formal'],required=True);args=p.parse_args()
    with (ROOT/'.measurement.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.stage=='formal' and not suite._read(ROOT/'progress-pilot.json')['complete']:
            raise ValueError('pilot incomplete')
        plan=ROOT/('plan-'+args.stage+'.json')
        if plan.exists():raise ValueError('existing plan retained; no silent retry')
        cases=cases_for(args.stage);suite._save(plan,dict(cases=cases),exclusive=True)
        progress=dict(planned=len(cases),complete=False,outcomes=[])
        target=ROOT/('progress-'+args.stage+'.json')
        for c in cases:
            peer_check();identity=suite.case_hash(c);log=ROOT/(identity+'.log');command=suite.native_command(c)
            item=dict(case_hash=identity,status='running',command=command,log_path=str(log),started_at=time.time())
            progress['outcomes'].append(item);suite._save(target,progress)
            with log.open('x') as f:process=subprocess.run(command,stdout=f,stderr=subprocess.STDOUT)
            lines=log.read_text().splitlines();paths=[s[7:] for s in lines if s.startswith('OUTPUT ')]
            item.update(exit_code=process.returncode,finished_at=time.time(),run_path=paths[0] if len(paths)==1 else None,status='failed')
            suite._save(target,progress)
            if process.returncode or len(paths)!=1 or not any(s.startswith('NATIVE_DONE ') for s in lines):
                raise ValueError('sender failure retained: '+str(log))
            try:item['audit']=validate(c,paths[0])
            except Exception as error:
                item['audit_error']=str(error);suite._save(target,progress);raise
            item['status']='success';suite._save(target,progress)
            print('MAIN_PROGRESS',args.stage,len(progress['outcomes']),len(cases),c['variant'],c['load'],flush=True)
        progress['complete']=True;suite._save(target,progress);print('MAIN_STAGE_COMPLETE',args.stage,flush=True)


if __name__=='__main__':main()
