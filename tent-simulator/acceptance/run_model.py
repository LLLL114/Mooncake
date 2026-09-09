#!/usr/bin/env python3
"""Fresh source-based original-policy trajectories; no new scheduler candidate."""

import sys as _sys
_sys.dont_write_bytecode = True
from pathlib import Path as _BootstrapPath
_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1] / "scripts"))
from experiment_paths import OUTPUT_ROOT
import argparse,gzip,hashlib,json,math,pathlib,random,subprocess,sys
HERE=pathlib.Path(__file__).resolve().parent;ROOT=HERE.parent;B=OUTPUT_ROOT/'build/release';OUT=OUTPUT_ROOT/'runs/acceptance-baseline/model'
sys.path.insert(0,str(ROOT/'scripts'))
from run_experiment_a import BASE
from metrics import np,q,dispersion
SEEDS=[1,19,12345]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--only-gate',action='store_true');args=parser.parse_args()
 OUT.mkdir(parents=True,exist_ok=True);index=[];rows=[]
 if args.only_gate:
  index=json.loads((OUT/'index.json').read_text());rows=json.loads((OUT/'rows.json').read_text())
 def save():
  (OUT/'index.json').write_text(json.dumps(index,indent=2)+'\n');(OUT/'rows.json').write_text(json.dumps(rows,indent=2,allow_nan=False)+'\n')
 def execute(name,c,program='acceptance_sim'):
  inp=OUT/(name+'.config.json');dest=OUT/(name+'.json');assert not inp.exists() and not dest.exists(),name
  inp.write_text(json.dumps(c)+'\n')
  r=subprocess.run(['taskset','-c','8',str(B/program),str(inp),str(dest)],capture_output=True,text=True,timeout=180)
  (OUT/(name+'.stderr')).write_text(r.stderr);assert r.returncode==0,(name,r.stderr[-1000:]);d=json.loads(dest.read_text())
  assert d['allocation_bytes']==d['completion_bytes']
  if c.get('kind')=='gate':
   assert program=='controlled'
   assert all(m['seen']>m['accepted']>0 and m['min_interval_ns']>=c['update_interval_ns'] for m in d['sample_meters']),name
  if program=='concurrency':assert d['quota_leaks']==0
  else:assert all(x['inflight']==0 for x in d['final_stats'])
  index.append({'name':name,'program':program,'config_sha256':sha(inp),'output_sha256':sha(dest)})
  if program=='concurrency':
   lat=d['request_latency_ns'][c['warmup_requests']:];row={'kind':'concurrent','workers':c['workers'],'burst':c['burst'],'seed':c['seed'],'p99_us':q(lat,.99)/1000,'assignment_step':d['client_allocation_step'],'weight_step':d['client_weight_step'],'goodput_gbps':sum(d['measurement_bytes'])*8/((c['requests']-c['warmup_requests'])*c['arrival_interval_ns']),'migration':None}
  else:
   times=np.asarray(c.get('request_arrivals_ns',list(range(0,c['requests']*c['arrival_interval_ns'],c['arrival_interval_ns']))));lat=np.asarray(d['request_latency_ns'])
   lo=c.get('measurement_start_ns',c['warmup_requests']*c['arrival_interval_ns']);hi=c.get('measurement_stop_ns',c['requests']*c['arrival_interval_ns']);seconds=(hi-lo)/1e9
   ww=[w for w in d['windows'] if lo<=w['start_ns'] and w['start_ns']+1000000<=hi]
   row={'kind':c['kind'],'size':c['request_bytes'],'topology':c['topology'],'load':c.get('offered_fraction',.9),'seed':c['seed'],'alpha':c['alpha'],'update_interval_ns':c.get('update_interval_ns',0),'poll_interval_ns':c['poll_interval_ns'],'aggregate_path':d['aggregate_path'],'split_count':d['split_count'],'p99_us':q(lat[(times>=lo)&(times<hi)],.99)/1000,'goodput_gbps':sum(sum(w['done']) for w in ww)*8/(len(ww)*1000000),'assignment_step':d['allocation_step'],'decision_weight_mean_step':d['decision_tv']/max(1,d['decision_count']-1),'decision_weight_tv_per_s':d['decision_tv']/seconds,'queue_peak_bytes':max(sum(w['queue_bytes']) for w in ww),'deadline_exceeded':d['deadline_exceeded'],'migration':None}
   if c['kind']=='gate':row.update(sampling_seen=sum(m['seen'] for m in d['sample_meters']),sampling_accepted=sum(m['accepted'] for m in d['sample_meters']),sampling_min_interval_ns=min(m['min_interval_ns'] for m in d['sample_meters']))
   coarse=[]
   for start in range(math.ceil(lo/10000000)*10000000,hi-10000000+1,10000000):
    w=[x for x in d['windows'][start//1000000:start//1000000+10]]
    if len(w)!=10:continue
    total=sum(sum(x['assigned']) for x in w)
    coarse.append({'time_ns':start,'share0':sum(x['assigned'][0] for x in w)/total if total else None,'goodput_gbps':sum(sum(x['done']) for x in w)*8/10000000,'queue_bytes':sum(w[-1]['queue_bytes'])})
   row['throughput_10ms']=dispersion([w['goodput_gbps'] for w in coarse]);row['share_10ms']=dispersion([w['share0'] for w in coarse])
   if 'phase_off_ns' in c:
    on=c['phase_on_ns'];off=c['phase_off_ns'];phases={}
    for label,start,stop in [('before',lo,on),('during',on,off),('after',off,hi)]:
     w=[x for x in d['windows'] if start<=x['start_ns']<stop];l=lat[(times>=start)&(times<stop)];assigned=sum(sum(x['assigned']) for x in w)
     phases[label]={'p99_us':q(l,.99)/1000,'goodput_gbps':sum(sum(x['done']) for x in w)*8/(stop-start),'share0':sum(x['assigned'][0] for x in w)/assigned,'queue_peak_bytes':max(sum(x['queue_bytes']) for x in w)}
    target=phases['before'];after=[w for w in coarse if w['time_ns']>=off];band=[]
    for w in after:band.append(w['share0'] is not None and abs(w['share0']-target['share0'])<=.05 and abs(w['goodput_gbps']/target['goodput_gbps']-1)<=.05 and w['queue_bytes']<=target['queue_peak_bytes']+1048576)
    settle=next((i for i in range(len(band)-9) if all(band[i:i+10])),None)
    row.update(phases=phases,recovery_ms=None if settle is None else (after[settle]['time_ns']-off)/1e6,recovery_confirmed_ms=None if settle is None else (after[settle+9]['time_ns']+10000000-off)/1e6,reexits_after_settle=None if settle is None else sum(band[i-1] and not band[i] for i in range(settle+10,len(band))),recovery_observation_ms=(hi-off)/1e6)
   (OUT/(name+'.windows.json')).write_text(json.dumps(coarse,allow_nan=False)+'\n')
  row['name']=name;rows.append(row)
  with gzip.open(str(dest)+'.gz','wb',compresslevel=1) as f:f.write(dest.read_bytes())
  index[-1]['gzip_sha256']=sha(pathlib.Path(str(dest)+'.gz'));dest.unlink();save();print('MODEL',len(rows),name,round(row['p99_us'],3),flush=True)
 matrix=[]
 for topo in ['equal','dual_numa']:
  for load in [.2,.6,.9]:matrix.append((1048576,topo,load,{},'steady'))
 for size in [524288,983040,999424]:matrix.append((size,'equal',.9,{},'threshold'))
 matrix.append((1048576,'equal',.9,{'rails':1,'capacity_bytes_per_second':[12.5e9]},'single'))
 for alpha in [.5,.99,.999]:matrix.append((1048576,'equal',.9,{'alpha':alpha},'alpha'))
 for gate in [1000000,5000000]:matrix.append((1048576,'equal',.9,{'update_interval_ns':gate},'gate'))
 for poll in [10000,100000]:matrix.append((1048576,'equal',.9,{'poll_interval_ns':poll},'cq'))
 cases=[(*m,seed) for m in matrix if not args.only_gate or m[-1]=='gate' for seed in SEEDS];random.Random(91003).shuffle(cases)
 for size,topo,load,extra,kind,seed in cases:
  rails=extra.get('rails',2);gap=round(size/(12.5e9*rails*load)*1e9);n=12000;warm=2000
  c={**BASE,'requests':n,'warmup_requests':warm,'request_bytes':size,'arrival_interval_ns':gap,'topology':topo,'seed':seed,'offered_fraction':load,'kind':kind,'allocation_mode':'original','request_trace_from':warm,'request_trace_to':n,**extra}
  execute(f'{kind}_{size}_{topo}_{load}_{str(extra)}_{seed}'.replace(' ','').replace('/','_'),c,'controlled' if kind=='gate' else 'acceptance_sim')
 if args.only_gate:
  print('GATE_REPAIR_DONE',len(rows),flush=True);return
 for kind,load,duration in [('load',.3,100000000),('load',.3,1000000000),('capacity',.6,1000000000),('capacity',.9,1000000000),('burst',.2,10000000)]:
  on=500000000;off=on+duration;end=off+2000000000
  segments=[(0,end,load)] if kind=='capacity' else [(0,on,load),(on,off,1.5 if kind=='burst' else .9),(off,end,load)]
  times=[]
  for start,stop,offered in segments:times.extend(range(start,stop,round(1048576/(25e9*offered)*1e9)))
  warm=sum(t<200000000 for t in times)
  events=[{'at_ns':on,'rail':0,'bytes_per_second':6.25e9},{'at_ns':off,'rail':0,'bytes_per_second':12.5e9}] if kind=='capacity' else []
  for seed in SEEDS:
   c={**BASE,'requests':len(times),'warmup_requests':warm,'request_arrivals_ns':times,'arrival_interval_ns':round(1048576/(25e9*load)*1e9),'measurement_start_ns':200000000,'measurement_stop_ns':end,'phase_on_ns':on,'phase_off_ns':off,'request_trace_from':warm,'request_trace_to':len(times),'capacity_events':events,'kind':kind,'offered_fraction':load,'seed':seed,'allocation_mode':'original'}
   execute(f'{kind}_{load}_{duration}_{seed}',c)
 for workers,burst in [(1,True),(8,True),(8,False)]:
  for seed in SEEDS:
   c={**BASE,'requests':4096,'warmup_requests':1024,'arrival_interval_ns':46603,'workers':workers,'processes':1,'burst':burst,'serialize':not burst,'seed':seed,'kind':'concurrent'}
   execute(f'concurrent_{workers}_{int(burst)}_{seed}',c,'concurrency')
 print('MODEL_DONE',len(rows),flush=True)
if __name__=='__main__':main()
