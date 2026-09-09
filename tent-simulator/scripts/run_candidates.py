#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import argparse,hashlib,json,math,os,pathlib,random,statistics,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from run_experiment_a import BASE,quantile,cv
POLICIES=['legacy','old_remainder','largest_remainder','earliest_finish','byte_deficit']
SEEDS=[1,2,19,12345,4294967295]
def policy_config(policy):return {'new_policy':policy if policy not in ['legacy','old_remainder'] else 'legacy','allocation_mode':'remainder' if policy=='old_remainder' else 'original','policy':policy}
def main():
 p=argparse.ArgumentParser();p.add_argument('--stage',choices=['validate','sim','real'],default='validate');p.add_argument('--sanitize',action='store_true');a=p.parse_args()
 b=OUTPUT_ROOT/'build'/('sanitize' if a.sanitize else 'release');out=OUTPUT_ROOT/'runs/candidate-algorithms'/('sanitize' if a.sanitize else a.stage);out.mkdir(parents=True,exist_ok=True)
 cases=[];checks=[];rows=[]
 def save():
  (out/'cases.json').write_text(json.dumps(cases,indent=2)+'\n');(out/'checks.json').write_text(json.dumps(checks,indent=2)+'\n')
 def execute(name,cfg,program):
  i=out/(name+'.config.json');o=out/(name+'.json');assert not i.exists() and not o.exists(),name;i.write_text(json.dumps(cfg)+'\n');env=dict(os.environ)
  if a.sanitize:env.update(ASAN_OPTIONS='detect_leaks=1:abort_on_error=1',UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
  r=subprocess.run(['taskset','-c','8',str(b/program),str(i),str(o)],capture_output=True,text=True,timeout=120,env=env);(out/(name+'.stderr')).write_text(r.stderr)
  if r.returncode:raise RuntimeError(name+': '+r.stderr)
  d=json.loads(o.read_text());assert all(s['inflight']==0 for s in d.get('final_stats',[]))
  if program in ['candidate_concurrent','concurrency']:assert d['quota_leaks']==0
  assert all(s['reserved']==0 and s['posted']==0 for s in d.get('new_policy_stats',[]))
  if program!='candidate_real':assert d['allocation_bytes']==d['completion_bytes']
  cases.append({'name':name,'program':program,'config_sha256':hashlib.sha256(i.read_bytes()).hexdigest(),'result_sha256':hashlib.sha256(o.read_bytes()).hexdigest()});return d
 def check(name,value):checks.append({'name':name,'passed':bool(value)});save();assert value,name;print('CHECK',name,flush=True)
 if a.stage!='real':
  env=dict(os.environ)
  if a.sanitize:env.update(ASAN_OPTIONS='detect_leaks=1:abort_on_error=1',UBSAN_OPTIONS='halt_on_error=1')
  r=subprocess.run([str(b/'candidate_tests')],capture_output=True,text=True,timeout=60,env=env);(out/'unit.log').write_text(r.stdout+r.stderr);check('library_unit_tests',r.returncode==0 and 'ALGORITHM_TESTS_OK' in r.stdout)
  overlap=subprocess.run([str(b/'capacity_overlap')],capture_output=True,text=True,timeout=30,env=env)
  (out/'capacity-overlap.log').write_text(overlap.stdout+overlap.stderr)
  values=overlap.stdout.split() if overlap.returncode==0 else []
  check('delayed_CQ_does_not_turn_injection_into_capacity',len(values)==4 and abs(float(values[0])-12.5e9)/12.5e9<.01 and int(values[2])==int(values[3])==0)
  cfg={**BASE,'requests':1024,'warmup_requests':256,'arrival_interval_ns':70000}
  for seed in SEEDS:
   old=execute('baseline_'+str(seed),{**cfg,'seed':seed},'dynamic');new=execute('legacy_'+str(seed),{**cfg,'seed':seed,**policy_config('legacy')},'candidate_sim')
   check('legacy_exact_parity_'+str(seed),old['digest']==new['digest'] and old['request_latency_ns']==new['request_latency_ns'])
  for policy in POLICIES[2:]:
   for size in [65536,999424,1048576]:
    d=execute(f'valid_{policy}_{size}',{**cfg,'request_bytes':size,**policy_config(policy)},'candidate_sim');check('quota_'+policy+'_'+str(size),d['deadline_exceeded']==0)
   d=execute('single_'+policy,{**cfg,'rails':1,'capacity_bytes_per_second':[12.5e9],**policy_config(policy)},'candidate_sim');check('single_rail_'+policy,d['completion_bytes'][0]==cfg['requests']*1048576)
  for seed in SEEDS:
   ecfg={**cfg,'workers':8,'processes':1,'seed':seed,**policy_config('legacy')}
   previous=execute('old_concurrent_'+str(seed),ecfg,'concurrency');current=execute('new_concurrent_'+str(seed),ecfg,'candidate_concurrent')
   check('concurrent_legacy_parity_'+str(seed),previous['request_latency_ns']==current['request_latency_ns'])
  save()
  if a.stage=='validate' or a.sanitize:print('CANDIDATES_VALIDATED',len(checks),len(cases),flush=True);return
 if a.stage=='sim':
  matrix=[]
  for size in [65536,999424,1048576,16777216]:
   for topo in ['equal','dual_numa']:
    for load in ([.6,.9] if size==16777216 else [.2,.6,.9]):
     for policy in POLICIES:
      for seed in SEEDS:matrix.append((size,topo,load,policy,seed))
  random.Random(9097).shuffle(matrix)
  for size,topo,load,policy,seed in matrix:
   gap=round(size/(25e9*load)*1e9);name=f'steady_{size}_{topo}_{load}_{policy}_{seed}'
   c={**BASE,'requests':12000,'warmup_requests':2000,'request_bytes':size,'topology':topo,'arrival_interval_ns':gap,'offered_fraction':load,'seed':seed,**policy_config(policy)}
   d=execute(name,c,'candidate_sim');lat=d['request_latency_ns'][2000:];w=[x for x in d['windows'] if x['start_ns']>=2000*gap and x['start_ns']+1000000<=12000*gap];duration=len(w)*.001;rates=[sum(x['done'][i] for x in w)/duration*8/1e9 for i in [0,1]]
   cohorts=[quantile(lat[i:i+200],.99) for i in range(0,len(lat)-199,200)]
   rows.append({'name':name,'kind':'steady','size':size,'topology':topo,'load':load,'policy':policy,'seed':seed,'goodput_gbps':sum(rates),'p99_us':quantile(lat,.99)/1000,'p99_cv':cv(cohorts),'allocation_step':d['allocation_step'],'jain':sum(rates)**2/(2*sum(x*x for x in rates)),'allocation_wall_ns':d['allocation_ns']/d['allocation_calls'],'module_wall_ns':d['new_policy_ns']/max(1,d['new_policy_calls']),'stats':d['new_policy_stats'],'deadline_exceeded':d['deadline_exceeded']})
   if len(rows)%20==0:print('STEADY_PROGRESS',len(rows),'/',len(matrix),flush=True);save()
  for load in [.6,.9]:
   for duration in [100000000,1000000000]:
    for policy in POLICIES:
     for seed in SEEDS:
      gap=round(1048576/(25e9*load)*1e9);end=500000000+duration+2000000000;times=list(range(0,end,gap));warm=sum(t<200000000 for t in times)
      name=f'drop_{load}_{duration}_{policy}_{seed}';c={**BASE,'requests':len(times),'warmup_requests':warm,'arrival_interval_ns':gap,'request_arrivals_ns':times,'measurement_start_ns':200000000,'measurement_stop_ns':end,'offered_fraction':load,'seed':seed,'capacity_events':[{'at_ns':500000000,'rail':0,'bytes_per_second':6.25e9},{'at_ns':500000000+duration,'rail':0,'bytes_per_second':12.5e9}],**policy_config(policy)}
      d=execute(name,c,'candidate_sim');phase={}
      for label,lo,hi in [('before',200000000,500000000),('during',500000000,500000000+duration),('after',500000000+duration,end)]:
       lat=[v for t,v in zip(times,d['request_latency_ns']) if lo<=t<hi];w=[x for x in d['windows'] if lo<=x['start_ns']<hi];phase[label]={'p99_us':quantile(lat,.99)/1000,'queue_peak':max(sum(x['queue_bytes']) for x in w),'goodput_gbps':sum(sum(x['done']) for x in w)*8/(hi-lo),'requests':len(lat)}
      rows.append({'name':name,'kind':'drop','size':1048576,'topology':'equal','load':load,'duration_ms':duration/1e6,'policy':policy,'seed':seed,'phases':phase,'allocation_step':d['allocation_step'],'allocation_wall_ns':d['allocation_ns']/d['allocation_calls'],'stats':d['new_policy_stats'],'deadline_exceeded':d['deadline_exceeded']})
      if len(rows)%20==0:print('DYNAMIC_PROGRESS',len(rows),flush=True);save()
  for workers in [1,2,4,8]:
   for burst in ([True] if workers==1 else [True,False]):
    for policy in POLICIES:
     for seed in SEEDS:
      gap=round(1048576/(25e9*.9)*1e9);name=f'concurrent_w{workers}_b{int(burst)}_{policy}_{seed}'
      c={**BASE,'requests':4096,'warmup_requests':1024,'request_bytes':1048576,'arrival_interval_ns':gap,'workers':workers,'processes':1,'burst':burst,'serialize':not burst,'seed':seed,**policy_config(policy)}
      d=execute(name,c,'candidate_concurrent');lat=d['request_latency_ns'][1024:];duration=3072*gap/1e9
      rows.append({'name':name,'kind':'concurrent','workers':workers,'burst':burst,'policy':policy,'seed':seed,'size':1048576,'topology':'equal','load':.9,'p99_us':quantile(lat,.99)/1000,'goodput_gbps':sum(d['measurement_bytes'])*8/duration/1e9,'allocation_step':d['client_allocation_step'],'allocation_wall_ns':d['allocation_ns']/d['allocation_calls'],'stats':d['new_policy_stats']})
      if len(rows)%20==0:print('CONCURRENT_PROGRESS',len(rows),flush=True);save()
 else:
  # Run this stage alone, after model/sanitizer work has finished.
  base={**BASE,'requests':4096,'warmup_requests':256,'request_bytes':1048576,'rail_mask':3,'arrival_interval_ns':0,'smart':False,'topology':'equal',**policy_config('legacy')}
  def real(name,c,kind):
   d=execute(name,c,'candidate_real');n=c['requests'];warm=c['warmup_requests'];lat=d['request_scheduled_latency_ns'][warm:];service=d['request_service_latency_ns'][warm:];shares=d['allocation_shares'][warm:]
   assert d['completion_count']==n*16 and sum(d['completion_bytes'])==n*1048576 and d['verified_final_slot_bytes']>0
   row={'name':name,'kind':kind,'size':1048576,'topology':c['topology'],'load':c.get('offered_fraction',0),'policy':c['policy'],'seed':c['seed'],'mask':c['rail_mask'],'goodput_gbps':sum(d['completion_bytes'])*8/d['elapsed_ns'],'p99_us':quantile(lat,.99)/1000,'service_p99_us':quantile(service,.99)/1000,'p99_cv':cv([quantile(lat[i:i+200],.99) for i in range(0,len(lat)-199,200)]),'allocation_step':sum(abs(x-y) for x,y in zip(shares,shares[1:]))/(len(shares)-1),'allocation_wall_ns':d['allocation_ns']/d['allocation_calls'],'module_wall_ns':d['new_policy_ns']/max(1,d['new_policy_calls']),'max_injection_lateness_us':d['max_injection_lateness_ns']/1000,'duration_s':d['elapsed_ns']/1e9,'stats':d['new_policy_stats']};rows.append(row);print('REAL',name,round(row['goodput_gbps'],3),round(row['p99_us'],3),flush=True);save();return row
  for mask in [1,2,3]:
   for seed in SEEDS:real(f'cal_{mask}_{seed}',{**base,'rail_mask':mask,'seed':seed},'calibration')
  capacity=statistics.median(r['goodput_gbps'] for r in rows if r['mask']==3)
  matrix=[(topo,load,policy,seed) for topo in ['equal','dual_numa'] for load in [.2,.6,.9] for policy in POLICIES for seed in SEEDS];random.Random(9098).shuffle(matrix)
  for topo,load,policy,seed in matrix:
   gap=round(1048576*8/(capacity*load));real(f'{topo}_{load}_{policy}_{seed}',{**base,'requests':12288,'warmup_requests':4096,'topology':topo,'offered_fraction':load,'arrival_interval_ns':gap,'smart':True,'seed':seed,**policy_config(policy)},'real')
  for topo in ['equal','dual_numa']:
   for policy in POLICIES:
    for seed in SEEDS:
     real(f'saturated_{topo}_{policy}_{seed}',{**base,'requests':12288,'warmup_requests':4096,'topology':topo,'offered_fraction':1.0,'arrival_interval_ns':0,'smart':True,'seed':seed,**policy_config(policy)},'saturated')
 save();(out/'summary.json').write_text(json.dumps({'rows':rows,'cases':len(cases),'checks':checks,'stage':a.stage,'parameters':'module defaults, no per-case parameter tuning','build':json.loads((b/'manifest-candidates.json').read_text()),'runner_sha256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()},indent=2)+'\n');print('CANDIDATES_DONE',a.stage,len(rows),len(cases),flush=True)
if __name__=='__main__':main()
