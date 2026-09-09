#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import argparse,hashlib,json,math,os,pathlib,random,statistics,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from run_experiment_a import BASE,quantile,cv
SEEDS=[1,2,19,12345,4294967295]
def main():
 p=argparse.ArgumentParser();p.add_argument('--sanitize',action='store_true');p.add_argument('--stage',choices=['E','F','validation'],default='validation');a=p.parse_args()
 b=OUTPUT_ROOT/'build'/('sanitize' if a.sanitize else 'release');out=OUTPUT_ROOT/'runs/experiment-ef'/('sanitize' if a.sanitize else a.stage);out.mkdir(parents=True,exist_ok=True)
 cases=[];checks=[];rows=[];cpu=next(c for c in sorted(os.sched_getaffinity(0)) if c>=8)
 def save():
  (out/'cases.json').write_text(json.dumps(cases,indent=2)+'\n');(out/'checks.json').write_text(json.dumps(checks,indent=2)+'\n')
 def run(name,cfg,program):
  inp=out/(name+'.config.json');dest=out/(name+'.json')
  if inp.exists() or dest.exists():raise FileExistsError(str(inp))
  inp.write_text(json.dumps(cfg)+'\n');env=dict(os.environ)
  if a.sanitize:env.update(ASAN_OPTIONS='detect_leaks=1:abort_on_error=1',UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
  r=subprocess.run(['taskset','-c',str(cpu),str(b/program),str(inp),str(dest)],capture_output=True,text=True,timeout=180,env=env)
  (out/(name+'.stderr')).write_text(r.stderr)
  if r.returncode:raise RuntimeError(name+': '+r.stderr)
  d=json.loads(dest.read_text());assert d['allocation_bytes']==d['completion_bytes']
  if program=='concurrency':assert d['quota_leaks']==0 and all(x>0 for x in d['request_latency_ns'])
  else:assert all(s['inflight']==0 for s in d['final_stats']) and d['trace_dropped']==0
  cases.append({'name':name,'program':program,'config_sha256':hashlib.sha256(inp.read_bytes()).hexdigest(),'output_sha256':hashlib.sha256(dest.read_bytes()).hexdigest()});return d
 def check(name,value):checks.append({'name':name,'passed':bool(value)});save();assert value,name;print('CHECK',name,flush=True)
 cfg={**BASE,'requests':1024,'warmup_requests':256,'arrival_interval_ns':70000,'offered_fraction':.6}
 for seed in SEEDS:
  original=run('old_'+str(seed),{**cfg,'seed':seed},'allocation')
  e=run('single_'+str(seed),{**cfg,'seed':seed,'workers':1,'processes':1},'concurrency')
  f=run('dynamic_'+str(seed),{**cfg,'seed':seed},'dynamic')
  check('one_worker_latency_parity_'+str(seed),original['request_latency_ns']==e['request_latency_ns'])
  check('arrival_extension_parity_'+str(seed),original['digest']==f['digest'])
 c={**cfg,'requests':64,'warmup_requests':0,'workers':8,'processes':1,'request_bytes':65536,'jitter':0,'arrival_interval_ns':20000}
 concurrent=run('forced_snapshot',c,'concurrency');serial=run('serialized_snapshot',{**c,'serialize':True},'concurrency')
 check('same_snapshot_herd',sum(r['share0'] for r in concurrent['decisions'][:8])==8)
 check('serialization_sees_reservations',sum(r['share0'] for r in serial['decisions'][:8])==4)
 for shared in [False,True]:
  d=run('logical_process_'+str(shared),{**cfg,'workers':2,'processes':4,'shared_view':shared},'concurrency')
  check('multi_view_drains_'+str(shared),sum(d['completion_bytes'])==cfg['requests']*cfg['request_bytes'])
 # Piecewise arrival trace explicitly changes timing, not completion replay.
 times=[i*80000 for i in range(512)]+[512*80000+i*50000 for i in range(512)]
 d=run('variable_arrival',{**cfg,'request_arrivals_ns':times,'measurement_start_ns':times[256],'measurement_stop_ns':times[-1]+50000},'dynamic')
 check('variable_arrivals_drain',d['slices']==16384)
 save()
 if a.sanitize or a.stage=='validation':print('EF_VALIDATION_DONE',len(checks),len(cases),flush=True);return
 if a.stage=='E':
  matrix=[]
  for processes,workers in [(1,1),(1,2),(1,4),(1,8),(2,1),(4,1),(2,4)]:
   for shared in ([False] if processes==1 else [False,True]):
    patterns=[(True,False)] if processes*workers==1 else [(True,False),(True,True),(False,True)]
    for burst,serial in patterns:
     for size in [65536,1048576]:
      for load in [.6,.9]:
       for seed in SEEDS:matrix.append((processes,workers,shared,burst,serial,size,load,seed))
  random.Random(9091).shuffle(matrix)
  for processes,workers,shared,burst,serial,size,load,seed in matrix:
   gap=round(size/(25e9*load)*1e9);name=f'e_p{processes}_w{workers}_g{int(shared)}_b{int(burst)}_s{int(serial)}_n{size}_l{load}_r{seed}'
   c={**BASE,'requests':4096,'warmup_requests':1024,'request_bytes':size,'arrival_interval_ns':gap,'processes':processes,'workers':workers,'shared_view':shared,'burst':burst,'serialize':serial,'seed':seed,'offered_fraction':load}
   d=run(name,c,'concurrency');lat=d['request_latency_ns'][1024:];duration=3072*gap/1e9;rate=[x*8/duration/1e9 for x in d['measurement_bytes']]
   cohorts=[quantile(lat[i:i+200],.99) for i in range(0,len(lat)-199,200)]
   rows.append({'name':name,'processes':processes,'workers':workers,'shared':shared,'burst':burst,'serialize':serial,'size':size,'load':load,'seed':seed,'p99_us':quantile(lat,.99)/1000,'p99_cv':cv(cohorts),'goodput_gbps':sum(rate),'offered_gbps':size/gap*8,'jain':sum(rate)**2/(2*sum(x*x for x in rate)),'allocation_step':d['client_allocation_step'],'weight_step':d['client_weight_step'],'switches_per_10k':d['client_switches_per_10k']})
   if len(rows)%20==0:print('E_PROGRESS',len(rows),'/',len(matrix),flush=True);save()
 else:
  policies={'original':(.01,True,'original'),'alpha99':(.99,True,'original'),'alpha999':(.999,True,'original'),'remainder':(.01,True,'remainder')}
  cache={}
  def arrivals(segments):
   times=[]
   for begin,end,load in segments:
    gap=round(1048576/(25e9*load)*1e9);times.extend(range(begin,end,gap))
   return times
  def dynamic(kind,load,duration,policy,seed):
   key=(kind,load,duration,policy,seed)
   if key in cache:return cache[key]
   alpha,smart,mode=policies[policy];on=500000000;off=on+duration;end=off+2000000000
   segments=[(0,end,load)] if kind!='load' else [(0,on,.3),(on,off,.9),(off,end,.3)]
   times=arrivals(segments);warm=sum(t<200000000 for t in times);events=[]
   if kind=='capacity':events=[{'at_ns':on,'rail':0,'bytes_per_second':6.25e9},{'at_ns':off,'rail':0,'bytes_per_second':12.5e9}]
   if kind=='held':events=[{'at_ns':on,'rail':0,'bytes_per_second':6.25e9}]
   name=f'f_{kind}_l{load}_d{duration}_{policy}_s{seed}'
   c={**BASE,'requests':len(times),'warmup_requests':warm,'arrival_interval_ns':round(1048576/(25e9*load)*1e9),'request_arrivals_ns':times,'measurement_start_ns':200000000,'measurement_stop_ns':end,'alpha':alpha,'smart':smart,'allocation_mode':mode,'seed':seed,'capacity_events':events,'policy':policy,'phase_on_ns':on,'phase_off_ns':off,'offered_fraction':load}
   d=run(name,c,'dynamic');windows=[w for w in d['windows'] if w['start_ns']>=200000000];lat=d['request_latency_ns'];phase={}
   for label,lo,hi in [('before',200000000,on),('during',on,off),('after',off,end)]:
    ll=[v for t,v in zip(times,lat) if lo<=t<hi];ww=[w for w in windows if lo<=w['start_ns']<hi]
    phase[label]={'requests':len(ll),'p99_us':quantile(ll,.99)/1000,'max_us':max(ll)/1000,'queue_peak_bytes':max(sum(w['queue_bytes']) for w in ww),'mean_share0':sum(w['assigned'][0] for w in ww)/sum(sum(w['assigned']) for w in ww),'goodput_gbps':sum(sum(w['done']) for w in ww)*8/((hi-lo)/1e9)/1e9}
   after=[w for w in windows if w['start_ns']>=off];reference=phase['before']['mean_share0'];settle=None;queue_settle=None;band=[];qband=[]
   baseline_queue=max(sum(w['queue_bytes']) for w in windows if 200000000<=w['start_ns']<on)
   for w in after:
    total=sum(w['assigned']);band.append(total>0 and abs(w['assigned'][0]/total-reference)<=.05)
    qband.append(sum(w['queue_bytes'])<=baseline_queue+1048576)
    if settle is None and len(band)>=10 and all(band[-10:]):settle=(w['start_ns']+1000000-off-9000000)/1e6
    if queue_settle is None and len(qband)>=10 and all(qband[-10:]):queue_settle=(w['start_ns']+1000000-off-9000000)/1e6
   baseline_band=[]
   for w in windows:
    if 200000000<=w['start_ns']<on:
     total=sum(w['assigned']);baseline_band.append(total>0 and abs(w['assigned'][0]/total-reference)<=.05)
   baseline_qualifies=any(all(baseline_band[i:i+10]) for i in range(len(baseline_band)-9))
   if kind=='held':settle=None;queue_settle=None
   row={'baseline_recovery_band_qualifies':baseline_qualifies,'name':name,'kind':kind,'load':load,'duration_ms':duration/1e6,'policy':policy,'seed':seed,'phases':phase,'allocation_step':d['allocation_step'],'deadline_exceeded':d['deadline_exceeded'],'mean_share_recovery_ms':settle,'queue_recovery_ms':queue_settle,'requests':len(times),'warmup_requests':warm,'recovery_reference_share':reference}
   rows.append(row);cache[key]=row
   if len(rows)%10==0:print('F_PROGRESS',len(rows),flush=True);save()
   return row
  matrix=[]
  for policy in policies:
   for seed in SEEDS:
    for duration in [1000000,10000000,100000000,1000000000]:
     matrix.append(('load',.3,duration,policy,seed))
     for load in [.2,.6,.9]:matrix.append(('capacity',load,duration,policy,seed))
    for load in [.2,.6,.9]:matrix.append(('held',load,1000000000,policy,seed))
  random.Random(9092).shuffle(matrix)
  for key in matrix:dynamic(*key)
 save();summary={'rows':rows,'cases':len(cases),'checks':checks,'cpu':cpu,'stage':a.stage,'build':json.loads((b/'manifest-ef.json').read_text()),'runner_sha256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()}
 (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');print('EF_STAGE_DONE',a.stage,len(rows),len(cases),flush=True)
if __name__=='__main__':main()
