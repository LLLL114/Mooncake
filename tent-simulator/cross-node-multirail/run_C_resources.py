#!/usr/bin/env python3
"""SSH-only C resource control; BOTH endpoints must explicitly use one lane."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import random
import socket
import statistics
import sys

import run_suite_collection_v2 as suite

BASE=Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')
PEER_HASH='1029698b8287b5fe2d7b28d9e3fd63c3febae5990800813153488cd71c62979d'


def peer_contract(peer):
    with socket.create_connection((peer,19930),timeout=10) as sock:
        with sock.makefile('rwb') as stream:
            stream.write(b'{"op":"hello"}\n');stream.flush()
            reply=json.loads(stream.readline(1048576))
    env=reply.get('environment',{})
    if (reply.get('ok') is not True or env.get('rdma_num_lanes')!=1
            or env.get('module_sha256')!=PEER_HASH or env.get('nic')!='erdma_0'
            or env.get('erdma_compat_mode')!='Y' or len(env.get('receiver_script_sha256',''))!=64):
        raise ValueError('requires the explicit matched lane=1 receiver before any data transfer')
    return {k:env[k] for k in ('rdma_num_lanes','module_sha256','nic','erdma_compat_mode','receiver_script_sha256')}


def check_peer_result(case,result):
    root=Path(result['runPath']);m=suite._read(root/'manifest.json')
    if any(m.get('receiver',{}).get(k)!=v for k,v in case['receiver_contract'].items()):
        raise ValueError('actual receiver differs from resource case contract')
    config=suite._read(root/'tent-config.json')
    if config['transports']['rdma'].get('num_lanes')!=1:
        raise ValueError('sender did not use one lane')
    if suite._read(root/'observer.json').get('incomplete') is not False:
        raise ValueError('incomplete observer')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=['pilot','calibrate','formal'],required=True)
    parser.add_argument('--output-root',type=Path,default=BASE/'followups-c-resources')
    parser.add_argument('--peer',default='10.0.1.251')
    parser.add_argument('--tent-library',default=str(BASE/'build-v2/libtent_shared.so'))
    parser.add_argument('--stream-library',default=str(BASE/'build-v2/stream_native.so'))
    args=parser.parse_args();args.output_root=suite._outside_repo(args.output_root)
    args.latency_window_ms=5000 if args.phase=='formal' else 1000
    capacity_path=BASE/'suites-extended/capacity.json'
    parent=suite.load_capacity(capacity_path,args);context=suite._context(args)
    contract=peer_contract(args.peer)
    reference=parent['sizes']['1048576'];rate=reference['reference_bytes_per_second'];q=reference['q']
    resource_path=args.output_root/'capacity.json';resource=None
    if args.phase=='formal':
        resource=suite._read(resource_path)
        if (resource['receiver_contract']!=contract or resource['q']!=q
                or {k:v for k,v in resource['context'].items() if k!='latency_window_ms'}
                !={k:v for k,v in context.items() if k!='latency_window_ms'}):
            raise ValueError('resource capacity configuration differs')
    cases=[]
    for repeat in range(1,{'pilot':1,'calibrate':3,'formal':5}[args.phase]+1):
        fractions=[.2,0] if args.phase=='formal' else [.2 if args.phase=='pilot' else 0]
        random.Random(20260916+repeat).shuffle(fractions)
        for fraction in fractions:
            args.seconds={'pilot':8,'calibrate':10,'formal':120}[args.phase]
            args.warmup=10 if args.phase=='formal' else 1
            phase='baseline' if args.phase=='formal' else 'C-resource-'+args.phase
            case=suite._case(args,context,phase,'D0',1048576,q,repeat,str(fraction),rate=fraction*rate,
                 reference=dict(parent_default_capacity_sha256=hashlib.sha256(capacity_path.read_bytes()).hexdigest(),
                                parent_default_d0_bytes_per_second=rate))
            load_label=f'{int(fraction*100)}pct' if fraction else 'saturated'
            case['parameters'].update(workers=1,arrival_alignment='staggered',
                label=f'C-resource-{args.phase}-{load_label}-r{repeat}')
            case.update(protocol='C-matched-lanes1-formal-v2' if args.phase=='formal' else 'C-matched-lanes1-v1',receiver_contract=contract,
                scope='C resource comparison: both endpoints lane=1; fixed input references default-lane capacity, not new physical utilization')
            if resource:
                case.update(mode_capacity=resource['median_bytes_per_second'],
                            offered_exceeds_mode_capacity=fraction*rate>resource['median_bytes_per_second'],
                            resource_capacity_sha256=hashlib.sha256(resource_path.read_bytes()).hexdigest())
            cases.append(case)
    args.output_root.mkdir(parents=True,exist_ok=True)
    with (args.output_root/'.suite.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        plan_path=args.output_root/('plan-'+args.phase+'.json');plan=dict(cases=cases)
        if plan_path.exists():
            if suite._read(plan_path)!=plan:raise ValueError('resource plan changed')
        else:suite._save(plan_path,plan,exclusive=True)
        progress=dict(phase=args.phase,complete=False,planned=len(cases),outcomes=[])
        progress_path=args.output_root/('progress-'+args.phase+'.json');suite._save(progress_path,progress)
        for case in cases:
            if peer_contract(args.peer)!=contract:raise ValueError('receiver changed during suite')
            result=suite.run_case(case,args.output_root,collect_unexpected_overload=args.phase=='formal')
            row=dict(case_hash=suite.case_hash(case),status=result['status'],run_path=result.get('runPath'),
                     goodput_bytes_per_second=result.get('goodput_bytes_per_second'),collection_review=result.get('collection_review'))
            progress['outcomes'].append(row)
            try:
                check_peer_result(case,result)
                accepted=result['status'] in ('success','completed_overload','completed_overload_unverified')
                accepted=accepted or (args.phase=='formal' and result.get('collection_review',{}).get('allowed') is True)
                if not accepted:raise ValueError(result.get('error','resource case failed'))
            except (OSError,ValueError,KeyError,TypeError) as error:
                row['error']=str(error);suite._save(progress_path,progress);print('RESOURCE_STOPPED',row,flush=True);return 1
            suite._save(progress_path,progress)
        if args.phase=='calibrate':
            values=[r['goodput_bytes_per_second'] for r in progress['outcomes']]
            if any(v is None or v<=0 for v in values):raise ValueError('invalid resource capacity')
            result=dict(protocol='C-resource-capacity-v1',context=context,receiver_contract=contract,q=q,
                        median_bytes_per_second=statistics.median(values),samples=progress['outcomes'],
                        limitation='three 10s closed-loop runs at fixed Q; reference, not proof of hardware peak')
            if resource_path.exists():
                if suite._read(resource_path)!=result:raise ValueError('resource capacity changed')
            else:suite._save(resource_path,result,exclusive=True)
        progress['complete']=True
        progress['all_normal_success']=all(r['status']=='success' for r in progress['outcomes'])
        suite._save(progress_path,progress)
        print('RESOURCE_PHASE_COMPLETE',args.phase,len(cases),'ALL_NORMAL',progress['all_normal_success'],flush=True)
    return 0


if __name__=='__main__':sys.exit(main())
