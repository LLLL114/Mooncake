#!/usr/bin/env python3
"""Separate latency calibration and planned-arrival replay; never reuse old CQ times."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import pathlib,json,subprocess,statistics,hashlib,sys,math
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from run_experiment_a import BASE,quantile
out=OUTPUT_ROOT/'runs/experiment-ef/replay';out.mkdir(parents=True,exist_ok=True);real=OUTPUT_ROOT/'runs/experiment-ef/real';summary=json.loads((real/'summary.json').read_text());cases=[];latency={}
def execute(name,cfg,program):
 i=out/(name+'.config.json');o=out/(name+'.json');assert not i.exists() and not o.exists();i.write_text(json.dumps(cfg)+'\n')
 r=subprocess.run(['taskset','-c','8',str(OUTPUT_ROOT/'build/release'/program),str(i),str(o)],capture_output=True,text=True,timeout=45);(out/(name+'.stderr')).write_text(r.stderr);assert r.returncode==0,(name,r.stderr)
 d=json.loads(o.read_text());cases.append({'name':name,'program':program,'config_sha256':hashlib.sha256(i.read_bytes()).hexdigest(),'output_sha256':hashlib.sha256(o.read_bytes()).hexdigest()});return d
rates={r:statistics.median(x['goodput_gbps'] for x in summary['rows'] if x['policy']=='calibration' and x['mask']==1<<r)*1e9/8 for r in [0,1]}
for rail in [0,1]:
 samples=[]
 for seed in [1,2,19,12345,4294967295]:
  cfg={**BASE,'requests':1024,'request_bytes':65536,'arrival_interval_ns':200000,'rail_mask':1<<rail,'smart':False,'seed':seed,'topology':'equal'}
  d=execute(f'latency_r{rail}_s{seed}',cfg,'real_rdma');samples+=d['request_service_latency_ns'][256:]
 latency[rail]=max(1,quantile(samples,.1)-math.ceil(65536/rates[rail]*1e9))
# Independent-rail model cannot represent an arbitrary shared bottleneck. Match
# only measured combined capacity by proportional scaling, clearly labelled.
scale=summary['calibrated_dual_Gbps']*1e9/8/sum(rates.values());capacity=[rates[r]*scale for r in [0,1]];base_latency=round(statistics.mean(latency.values()));rows=[]
for rr in summary['rows']:
 if rr['policy']=='calibration' or rr['seed']!=1:continue
 raw=json.loads((real/(rr['name']+'.json')).read_text());c=raw['config'];gap=c['arrival_interval_ns'];n=c['requests']
 cfg={**BASE,'requests':n,'warmup_requests':256,'arrival_interval_ns':gap,'capacity_bytes_per_second':capacity,'base_latency_ns':base_latency,'topology':c['topology'],'allocation_mode':c['allocation_mode'],'seed':1,'request_arrivals_ns':[r*gap for r in range(n)],'measurement_start_ns':256*gap,'measurement_stop_ns':n*gap}
 d=execute('replay_'+rr['name'],cfg,'dynamic');p99=quantile(d['request_latency_ns'][256:],.99)/1000
 rows.append({'name':rr['name'],'policy':rr['policy'],'topology':rr['topology'],'load':rr['offered_fraction'],'model_p99_us':p99,'real_scheduled_p99_us':rr['scheduled_p99_us'],'real_service_p99_us':rr['service_p99_us'],'ratio_to_scheduled':p99/rr['scheduled_p99_us'],'ratio_to_service':p99/rr['service_p99_us'],'real_max_injection_lateness_us':rr['max_injection_lateness_us']})
(out/'cases.json').write_text(json.dumps(cases,indent=2)+'\n');(out/'summary.json').write_text(json.dumps({'rows':rows,'latency_residual_ns':latency,'capacity_Bps':capacity,'combined_capacity_scale':scale,'base_latency_ns':base_latency,'cases':len(cases),'scope':'descriptive same-host calibration, fixed planned-arrival replay; real completion times are not simulator input; independent-rail approximation'},indent=2)+'\n');print('REPLAY_DONE',len(rows),len(cases),flush=True)
