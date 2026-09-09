#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import json,pathlib,subprocess,os,hashlib,statistics,random,math,sys
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from run_experiment_a import BASE,quantile,cv
out=OUTPUT_ROOT/'runs/experiment-ef/real';out.mkdir(parents=True,exist_ok=True);rows=[];cases=[]
def run(name,cfg):
 ip=out/(name+'.config.json');op=out/(name+'.json');assert not ip.exists() and not op.exists()
 ip.write_text(json.dumps(cfg)+'\n');r=subprocess.run(['taskset','-c','8',str(OUTPUT_ROOT/'build/release/real_rdma'),str(ip),str(op)],capture_output=True,text=True,timeout=45)
 (out/(name+'.stderr')).write_text(r.stderr)
 if r.returncode:raise RuntimeError(name+': '+r.stderr)
 d=json.loads(op.read_text());assert all(x['inflight']==0 for x in d['final_stats']) and d['completion_count']==cfg['requests']*(cfg['request_bytes']//65536)
 lat=d['request_scheduled_latency_ns'][256:];service=d['request_service_latency_ns'][256:]
 row={'name':name,'mask':cfg['rail_mask'],'policy':cfg['policy'],'topology':cfg['topology'],'seed':cfg['seed'],'size':cfg['request_bytes'],'offered_fraction':cfg.get('offered_fraction'),
      'goodput_gbps':sum(d['completion_bytes'])*8/d['elapsed_ns'],'scheduled_p99_us':quantile(lat,.99)/1000,'service_p99_us':quantile(service,.99)/1000,'max_injection_lateness_us':d['max_injection_lateness_ns']/1000,'allocation_step':d['allocation_step'],'p99_cohort_cv':cv([quantile(lat[i:i+200],.99) for i in range(0,len(lat)-199,200)]),'duration_seconds':d['elapsed_ns']/1e9,'verified_final_slot_bytes':d['verified_final_slot_bytes'],'completed_bytes':sum(d['completion_bytes']),'devices':d['devices']}
 rows.append(row);cases.append({'name':name,'config_sha256':hashlib.sha256(ip.read_bytes()).hexdigest(),'output_sha256':hashlib.sha256(op.read_bytes()).hexdigest()})
 (out/'cases.json').write_text(json.dumps(cases,indent=2)+'\n');print('REAL_CASE',name,round(row['goodput_gbps'],3),round(row['scheduled_p99_us'],3),flush=True);return row
base={**BASE,'requests':4096,'warmup_requests':256,'request_bytes':1048576,'rail_mask':3,'arrival_interval_ns':0,'smart':False,'topology':'equal','policy':'calibration'}
for mask in [1,2,3]:
 for seed in [1,2,19,12345,4294967295]:run(f'cal_m{mask}_s{seed}',{**base,'rail_mask':mask,'seed':seed})
capacity=statistics.median(r['goodput_gbps'] for r in rows if r['mask']==3)
matrix=[(topology,load,policy,seed) for topology in ['equal','dual_numa'] for load in [.2,.6,.9] for policy in ['original','remainder'] for seed in [1,2,19,12345,4294967295]]
random.Random(9093).shuffle(matrix)
for topology,load,policy,seed in matrix:
 gap=round(1048576*8/(capacity*load))
 run(f'{topology}_l{load}_{policy}_s{seed}',{**base,'smart':True,'topology':topology,'offered_fraction':load,'arrival_interval_ns':gap,'allocation_mode':policy,'policy':policy,'seed':seed})
(out/'summary.json').write_text(json.dumps({'rows':rows,'calibrated_dual_Gbps':capacity,'cases':len(cases),'build':json.loads((OUTPUT_ROOT/'build/release/manifest-real.json').read_text()),'limitations':'single-host direct selector+verbs harness; full TENT Worker/endpoint runtime bypassed; short runs; CPU8 shared host; same source buffer registered on both NICs; final ring-slot verification, not per-request unique payload'},indent=2)+'\n')
print('REAL_DONE',len(rows),flush=True)
