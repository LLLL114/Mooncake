#!/usr/bin/env python3
"""Original policy only. No device shutdown, link mutation, or production retries."""

import sys as _sys
_sys.dont_write_bytecode = True
from pathlib import Path as _BootstrapPath
_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1] / "scripts"))
from experiment_paths import OUTPUT_ROOT
import argparse,gzip,hashlib,json,math,os,pathlib,random,shutil,subprocess,sys,time
HERE=pathlib.Path(__file__).resolve().parent;ROOT=HERE.parent;B=OUTPUT_ROOT/'build/release';OUT=OUTPUT_ROOT/'runs/acceptance-baseline'
sys.path.insert(0,str(ROOT/'scripts'))
from run_experiment_a import BASE
from metrics import summarize,np
SEEDS=[1,2,19,12345,4294967295]
def sha(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--stage',choices=['validate','real'],required=True);args=ap.parse_args()
    out=OUT/args.stage;out.mkdir(parents=True,exist_ok=True);index=[];rows=[]
    def save():
        (out/'index.json').write_text(json.dumps(index,indent=2)+'\n')
        (out/'rows.json').write_text(json.dumps(rows,indent=2,allow_nan=False)+'\n')
    def execute(name,c,program='acceptance_real'):
        path=out/(name+'.json');cfg=out/(name+'.config.json');assert not path.exists() and not cfg.exists(),name
        cfg.write_text(json.dumps(c)+'\n');log=out/(name+'.stderr')
        with log.open('w') as err:
            p=subprocess.run(['taskset','-c','8',str(B/program),str(cfg),str(path)],stdout=err,stderr=err,timeout=180)
        if p.returncode:raise RuntimeError(name+': '+log.read_text()[-2000:])
        d=json.loads(path.read_text());assert all(s['inflight']==0 for s in d.get('final_stats',[]))
        index.append({'name':name,'program':program,'config_sha256':sha(cfg),'output_sha256':sha(path)})
        save();return d,path
    def raw_archive(d,path):
        raw=pathlib.Path(d['raw_trace']);gz=raw.with_suffix(raw.suffix+'.gz');index[-1]['raw_sha256']=sha(raw)
        with raw.open('rb') as src,gzip.open(gz,'wb',compresslevel=1) as dest:shutil.copyfileobj(src,dest,1024*1024)
        index[-1]['raw_gzip_sha256']=sha(gz);raw.unlink();save()
    def real(name,c,capacity,analyse=True):
        d,p=execute(name,c);assert d['completion_count']==d['requests_completed']*d['split_count']
        assert sum(d['completion_bytes'])==d['requests_completed']*c['request_bytes'] and d['verified_final_slot_bytes']>0
        if analyse:
            row,win=summarize(d,d['raw_trace'],capacity);row['name']=name;rows.append(row)
            (out/(name+'.windows.json')).write_text(json.dumps(win,allow_nan=False)+'\n')
            print('REAL',len(rows),name,'G',round(row['goodput_gbps'],3),'P99',round(row['p99_us'],3),'windows',row['p99_windows']['n'],flush=True)
        else:row=None
        raw_archive(d,p);return d,row
    if args.stage=='validate':
        checks=[]
        def check(name,ok):
            checks.append({'name':name,'passed':bool(ok)});(out/'checks.json').write_text(json.dumps(checks,indent=2)+'\n');assert ok,name;print('CHECK',name,flush=True)
        cfg={**BASE,'requests':2048,'warmup_requests':256,'arrival_interval_ns':70000,'allocation_mode':'original'}
        for size in [65536,524288,983040,999424,1048576]:
            for seed in [1,19]:
                c={**cfg,'request_bytes':size,'seed':seed};a,_=execute(f'old_{size}_{seed}',c,'dynamic');b,_=execute(f'new_{size}_{seed}',c,'acceptance_sim')
                check(f'original_exact_parity_{size}_{seed}',a['digest']==b['digest'] and a['request_latency_ns']==b['request_latency_ns'] and a['allocation_bytes']==b['allocation_bytes'])
                check(f'threshold_{size}_{seed}',b['aggregate_path']==(size>=999424))
        c={**BASE,'requests':4096,'request_bytes':1048576,'arrival_interval_ns':0,'fixed_count':True,'measurement_start_ns':0,'measurement_stop_ns':1000000000,'topology':'equal','rail_mask':3,'kind':'smoke','seed':1}
        d,_=real('real_ring_smoke',c,{'1':100.,'2':100.,'3':200.},False)
        check('real_completion_and_ring_conservation',d['requests_completed']==4096 and d['completion_count']==65536)
        print('VALIDATION_DONE',len(checks),flush=True);return
    # A held baseline reference and all calib/main configurations are original policy.
    base={**BASE,'allocation_mode':'original','rail_mask':3,'arrival_interval_ns':0,'fixed_count':False,'metric_weights':True}
    capacities={};calrows=[]
    for size in [65536,524288,1048576]:
        caps={}
        for mask in [1,2,3]:
            vals=[]
            for seed in SEEDS:
                stop=2000000000;c={**base,'request_bytes':size,'requests':math.ceil(stop*35/(size))+1024,'measurement_start_ns':1000000000,'measurement_stop_ns':stop,'smart':False,'seed':seed,'rail_mask':mask,'kind':'calibration','topology':'equal'}
                d,_=real(f'cal_{size}_{mask}_{seed}',c,{},False)
                rate=sum(sum(w) for w in d['done_50ms'][20:40])*8/1e9;vals.append(rate)
                calrows.append({'size':size,'mask':mask,'seed':seed,'goodput_gbps':rate,'devices':d['devices']})
                print('CAL',size,mask,seed,round(rate,3),flush=True)
            caps[str(mask)]=float(np.median(vals))
        capacities[str(size)]=caps
        (out/'calibration.json').write_text(json.dumps({'capacities':capacities,'runs':calrows},indent=2)+'\n')
    # Measurement perturbation: same program with weight observer on/off; all other bookkeeping identical.
    overhead=[(size,on,seed) for size in [65536,1048576] for seed in SEEDS for on in [False,True]]
    random.Random(91001).shuffle(overhead)
    for size,on,seed in overhead:
        cap=capacities[str(size)];gap=round(size*8/(cap['3']*.9));stop=1500000000
        c={**base,'requests':math.ceil(stop/gap),'request_bytes':size,'arrival_interval_ns':gap,'measurement_start_ns':500000000,'measurement_stop_ns':stop,'metric_weights':on,'seed':seed,'topology':'equal','kind':'overhead_on' if on else 'overhead_off','offered_fraction':.9}
        real(f'overhead_{size}_{int(on)}_{seed}',c,cap)
    matrix=[]
    for size in [65536,1048576]:
        for load in [.2,.6,.9]:
            matrix.append((size,'equal',load,3,'steady'))
    for load in [.2,.6,.9]:matrix.append((1048576,'dual_numa',load,3,'steady'))
    for size in [65536,524288,1048576]:matrix.append((size,'equal',1.,3,'saturated'))
    matrix.append((1048576,'dual_numa',1.,3,'saturated'))
    for mask in [1,2]:matrix.append((1048576,'equal',1.,mask,'single'))
    cases=[(*m,seed) for m in matrix for seed in SEEDS];random.Random(91002).shuffle(cases)
    for size,topo,load,mask,kind,seed in cases:
        caps=capacities[str(size)];gap=round(size*8/(caps[str(mask)]*load)) if kind=='steady' else 0;stop=11000000000
        requests=math.ceil(stop/gap) if gap else math.ceil(stop*35/size)+1024
        c={**base,'requests':requests,'request_bytes':size,'arrival_interval_ns':gap,'measurement_start_ns':1000000000,'measurement_stop_ns':stop,'seed':seed,'topology':topo,'kind':kind,'rail_mask':mask,'offered_fraction':load}
        real(f'{kind}_{size}_{topo}_{load}_{mask}_{seed}',c,caps)
    save();print('BASELINE_REAL_DONE',len(index),len(rows),flush=True)
if __name__=='__main__':main()
