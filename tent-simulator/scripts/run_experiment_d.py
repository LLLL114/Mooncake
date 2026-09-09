#!/usr/bin/env python3
"""Allocation transfer maps, probe accounting, and paired closed-loop diagnostics."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import argparse
import hashlib
import json
import math
import os
import pathlib
import random
import statistics
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from run_experiment_a import BASE, quantile, cv
SEEDS = [1, 2, 19, 12345, 4294967295]
MODES = ['original', 'remainder', 'no_probe', 'refreshed_greedy']


def summarize(d):
    c=d['config']; gap=c['arrival_interval_ns']; warm=c['warmup_requests']
    rails=c['rails']; window=c['window_ns']; start=warm*gap; stop=c['requests']*gap
    windows=[w for w in d['windows'] if start<=w['start_ns'] and w['start_ns']+window<=stop]
    duration=len(windows)*window/1e9
    rate=[sum(w['done'][i] for w in windows)/duration for i in range(rails)]
    lat=d['request_latency_ns'][warm:]
    cohorts=[quantile(lat[i:i+200],.99) for i in range(0,len(lat)-199,200)]
    group=max(1, math.ceil(20*gap/window))
    flows=[sum(sum(w['done']) for w in windows[i:i+group]) for i in range(0,len(windows)-group+1,group)]
    assert len(flows)>=20 and cohorts
    u=[r/12.5e9 for r in rate]
    return {'mode':c['allocation_mode'],'rails':rails,'size':c['request_bytes'],
            'load':c['offered_fraction'],'alpha':c['alpha'],'jitter':c['jitter'],'seed':c['seed'],
            'slice_count':d['split_count'],'aggregate':d['aggregate_path'],
            'goodput_gbps':sum(rate)*8/1e9,'offered_gbps':c['request_bytes']/gap*8,
            'p99_us':quantile(lat,.99)/1000,'p50_us':quantile(lat,.5)/1000,
            'p99_cohort_cv':cv(cohorts),'throughput_cv':cv(flows),'flow_bin_ms':group*window/1e6,
            'request_weight_step':d['d_request_first_weight_step'],
            'allocation_step':d['allocation_step'],'allocation_std':d['allocation_rail0_std'],
            'jain':sum(u)**2/(rails*sum(x*x for x in u)),
            'utilization':sum(u)/rails,'rail_goodput_gbps':[r*8/1e9 for r in rate],
            'probe_count':d['d_probe_count'],'probe_bytes':d['d_probe_bytes'],
            'batch_count':d['d_batch_count'],'batch_bytes':d['d_batch_bytes'],
            'allocation_histogram':d['allocation_histogram'],
            'deadline_exceeded':d['deadline_exceeded'],'digest':d['digest'],
            'queue_mean_bytes':statistics.mean(sum(w['queue_bytes']) for w in windows),
            'first_slice_switches_per_10k':d['first_slice_rail_changes']*10000/len(lat),
            'request_decisions':d['request_decisions'],'batches':d['d_batches']}


def main():
    p=argparse.ArgumentParser();p.add_argument('--sanitize',action='store_true');p.add_argument('--validate-only',action='store_true')
    args=p.parse_args()
    build=OUTPUT_ROOT/'build'/('sanitize' if args.sanitize else 'release')
    out=OUTPUT_ROOT/'runs/experiment-d'/('sanitize' if args.sanitize else 'run')
    out.mkdir(parents=True,exist_ok=True)
    cases=[]; checks=[]; rows=[]; fixtures=[]; probes=[]; geometry=[]
    cpu=next(c for c in sorted(os.sched_getaffinity(0)) if c>=8)
    def save():
        (out/'case-manifest.json').write_text(json.dumps(cases,indent=2)+'\n')
        (out/'validation.json').write_text(json.dumps(checks,indent=2)+'\n')
    def execute(name,cfg,program='allocation'):
        inp=out/(name+'.config.json'); dest=out/(name+'.json')
        if inp.exists() or dest.exists(): raise FileExistsError(str(inp))
        inp.write_text(json.dumps(cfg,indent=2)+'\n')
        env=dict(os.environ)
        if args.sanitize: env.update(ASAN_OPTIONS='detect_leaks=1:abort_on_error=1',UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
        r=subprocess.run(['taskset','-c',str(cpu),str(build/program),str(inp),str(dest)],capture_output=True,text=True,timeout=180,env=env)
        (out/(name+'.stderr')).write_text(r.stderr)
        if r.returncode: raise RuntimeError(name+': '+r.stderr)
        data=json.loads(dest.read_text())
        assert data.get('trace_dropped',0)==0 and all(x['inflight']==0 for x in data['final_stats'])
        if not cfg.get('allocation_fixture'):
            assert data['allocation_bytes']==data['completion_bytes']
            assert len(data['request_latency_ns'])==cfg['requests'] and data['deadline_exceeded']==0
        cases.append({'name':name,'program':program,'input_sha256':hashlib.sha256(inp.read_bytes()).hexdigest(),
                      'result_sha256':hashlib.sha256(dest.read_bytes()).hexdigest()})
        return data
    def check(name,value):
        checks.append({'name':name,'passed':bool(value)});save();assert value,name
        print('CHECK',name,'PASS',flush=True)
    def fixture(name,**extra):
        return execute(name,{'allocation_fixture':True,'jitter':0,'seed':1,'qos':True,**extra})
    cfg={**BASE,'requests':1000,'warmup_requests':200,'arrival_interval_ns':50000}
    for seed in SEEDS:
        a=execute('validate_old_'+str(seed),{**cfg,'seed':seed},'observed')
        b=execute('validate_new_'+str(seed),{**cfg,'seed':seed})
        check('uninstrumented_allocation_parity_'+str(seed),a['digest']==b['digest'] and a['request_latency_ns']==b['request_latency_ns'])
    lo=fixture('validate_lo',bandwidth_gbps=[98,102])
    hi=fixture('validate_hi',bandwidth_gbps=[102,98])
    fixed=fixture('validate_remainder',bandwidth_gbps=[98,102],allocation_mode='remainder')
    check('known_49_51_rounding',lo['rows'][0]['counts']==[7,9] and hi['rows'][0]['counts']==[9,7] and fixed['rows'][0]['counts']==[8,8])
    for mode in MODES+['frozen_greedy']:
        d=fixture('validate_probe_'+mode,calls=101,bandwidth_gbps=[98,102],allocation_mode=mode)
        rr=d['rows'][98:101]
        check('probe_99_100_101_'+mode,[x['probe'] for x in rr]==([False]*3 if mode=='no_probe' else [False,True,False]))
        if mode!='no_probe':check('probe_keeps_even_split_'+mode,rr[1]['counts']==[8,8])
    f=fixture('validate_frozen',allocation_mode='frozen_greedy')
    r=fixture('validate_refresh',allocation_mode='refreshed_greedy')
    check('matched_greedy_snapshot',f['rows'][0]['counts']==[16,0] and r['rows'][0]['counts']==[8,8])
    t=fixture('validate_tail',request_lengths=[1048575])
    check('tail_charge_preserved',t['rows'][0]['total_charge']==1048576 and sum(t['rows'][0]['payload_bytes'])==1048575)
    for rails in [1,4,8]:
        d=fixture('validate_rails_'+str(rails),rails=rails,allocation_mode='remainder')
        check('multi_rail_conservation_'+str(rails),sum(d['rows'][0]['counts'])==16)
    for mode in ['remainder','no_probe','frozen_greedy','refreshed_greedy']:
        d=execute('validate_closed_'+mode,{**cfg,'allocation_mode':mode})
        check('closed_drains_'+mode,d['slices']==16000)
    noobs=execute('validate_no_window',{**cfg,'window_ns':0})
    check('observer_only_noninvasive',noobs['digest']==b['digest'] if cfg['seed']==SEEDS[-1] else noobs['digest']==json.loads((out/'validate_new_1.json').read_text())['digest'])
    save()
    if args.sanitize or args.validate_only:
        print('D_VALIDATION_DONE',len(checks),len(cases),flush=True);return
    # Static transfer maps: do not feed service completions or learn bandwidth.
    for rails in [2,4,8]:
        for count in ([2,3,8,15,16,17,32,64] if rails==2 else [2,3,8,16,32]):
            for delta in ([-.03,-.01,-.001,-.000001,0,.000001,.001,.01,.03] if rails==2 else [-.001,0,.001]):
                p0=1/rails+delta; bw=[p0*rails*100]+[(1-p0)*rails*100/(rails-1)]*(rails-1)
                for mode in ['original','remainder']:
                    name=f'map_r{rails}_n{count}_d{delta}_m{mode}'
                    d=fixture(name,rails=rails,production_split=False,slice_count=count,
                              request_lengths=[count*65536],bandwidth_gbps=bw,allocation_mode=mode)
                    fixtures.append({'name':name,'rails':rails,'mode':mode,'delta':delta,**d['rows'][0]})
    lengths=[983040,983041,999423,999424,1032192,1048575,1048576,1048577,2097153,16777216]
    for length in lengths:
        d=fixture('geometry_'+str(length),request_lengths=[length]);geometry.append(d['rows'][0])
    # Probe payload depends on size at the MULTI-allocation counter phase.
    for label,lengths in [('fixed',[1048576]),('large_on_probe',[1048576]*99+[16777216]),
                         ('small_on_probe',[16777216]*99+[1048576]),('interleaved_single',[65536,1048576])]:
        for mode in ['original','no_probe']:
            d=fixture('probe_mix_'+label+'_'+mode,request_lengths=lengths,calls=1000,allocation_mode=mode)
            rr=d['rows'];selected=[x for x in rr if x['probe']];batch=[x for x in rr if x['aggregate']]
            probes.append({'pattern':label,'mode':mode,'requests':len(rr),'batch_calls':len(batch),
                           'probe_calls':len(selected),'probe_bytes':sum(x['length'] for x in selected),
                           'total_bytes':sum(x['length'] for x in rr),
                           'batch_bytes':sum(x['length'] for x in batch),
                           'probe_request_indices':[x['request'] for x in selected]})
    cache=set(); matrix=[]
    def add(phase,rails,size,load,mode,alpha=.01,jitter=1e-9):
        for seed in SEEDS:
            key=(rails,size,load,mode,alpha,jitter,seed)
            if key not in cache:cache.add(key);matrix.append((phase,key))
    for load in [.2,.6,.9]:
        for mode in MODES:add('main',2,1048576,load,mode)
        for mode in ['original','remainder','no_probe']:add('frozen_bw',2,1048576,load,mode,alpha=1)
    # Greedy pair has identical argmin, probe, charges and zero jitter. Only
    # within-request state refresh differs; baseline-vs-greedy is a composite.
    for load in [.6,.9]:
        for mode in ['original','frozen_greedy','refreshed_greedy']:add('snapshot',2,1048576,load,mode,jitter=0)
    for size in [983040,999424,1032192,1048575,1114112,2097152,16777216]:
        for load in [.6,.9]:
            for mode in ['original','remainder','no_probe']:add('size',2,size,load,mode)
    for size in [1048576,2097152]:
        for load in [.6,.9]:
            for mode in ['original','remainder','no_probe']:add('multi',4,size,load,mode)
    random.Random(9084).shuffle(matrix)
    for phase,(rails,size,load,mode,alpha,jitter,seed) in matrix:
        gap=round(size/(rails*12.5e9*load)*1e9)
        cfg={**BASE,'rails':rails,'capacity_bytes_per_second':[12.5e9]*rails,'request_bytes':size,
             'requests':20000,'warmup_requests':4000,'offered_fraction':load,'arrival_interval_ns':gap,
             'allocation_mode':mode,'alpha':alpha,'jitter':jitter,'seed':seed,
             'request_trace_from':4000,'request_trace_to':4300}
        name=f'closed_r{rails}_b{size}_l{load}_m{mode}_a{alpha}_j{jitter}_s{seed}'
        d=execute(name,cfg);row=summarize(d);row.update(name=name,phase=phase);rows.append(row)
        if len(rows)%15==0:print('D_MATRIX_PROGRESS',len(rows),'/',len(matrix),flush=True);save()
    # Historical A parity, when raw cases are available. All-off old/new parity
    # above remains self-contained even without old untracked artifacts.
    history=[]
    for row in rows:
        if row['phase']=='main' and row['mode']=='original':
            old=OUTPUT_ROOT/f"runs/experiment-a/run/equal_1048576_{int(row['load']*100)}_{row['seed']}_smart.json"
            if old.exists():
                prior=json.loads(old.read_text());check('A_digest_'+row['name'],prior['digest']==row['digest']);history.append(str(old))
    files=['simulator/driver.cpp','simulator/hooks.h','simulator/sampling.h','simulator/driver_d.cpp',
           'simulator/allocation.h','scripts/build.py','scripts/build_experiment_d.py',
           'scripts/run_experiment_a.py','scripts/run_experiment_d.py']
    result={'rows':rows,'fixtures':fixtures,'probes':probes,'geometry':geometry,'checks':checks,
            'matrix_count':len(rows),'case_count':len(cases),'cpu':cpu,'historical_A_pairs':len(history),
            'git_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'source_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files},
            'binary_sha256':{p:hashlib.sha256((build/p).read_bytes()).hexdigest() for p in ['allocation','observed']},
            'build':json.loads((build/'manifest-d.json').read_text())}
    check('unique_artifacts',len({c['name'] for c in cases})==len(cases));save()
    (out/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print('EXPERIMENT_D_DONE',len(rows),len(cases),len(checks),flush=True)

if __name__=='__main__':main()
