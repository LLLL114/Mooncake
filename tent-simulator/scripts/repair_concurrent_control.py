#!/usr/bin/env python3
"""Re-run the E subgroup after fixing its legacy remainder switch wiring."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import pathlib,json,subprocess,hashlib,shutil,statistics,sys
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from run_experiment_a import quantile
run=OUTPUT_ROOT/'runs/candidate-algorithms';stage=run/'sim';correction=run/'concurrent-control-correction'
before=json.loads((correction/'manifest-before.json').read_text());after=json.loads((OUTPUT_ROOT/'build/release/manifest-candidates.json').read_text())
unchanged=['candidate_sim','candidate_real','candidate_tests','capacity_overlap']
for name in unchanged:assert before['binary_sha256'][name]==after['binary_sha256'][name],name
proof={'unaffected_binaries_identical':unchanged,'reason':'The generated E driver did not load allocation_mode, so old_remainder incorrectly ran legacy. All 175 concurrent matrix cases were isolated and repeated.'}
summary=json.loads((stage/'summary.json').read_text());cases=json.loads((stage/'cases.json').read_text());affected=[r for r in summary['rows'] if r['kind']=='concurrent'];assert len(affected)==175
old_cases=correction/'old-cases';old_cases.mkdir();names={r['name'] for r in affected}
(correction/'cases-before.json').write_text(json.dumps([c for c in cases if c['name'] in names],indent=2)+'\n')
new_cases=[c for c in cases if c['name'] not in names];rows=[r for r in summary['rows'] if r['kind']!='concurrent']
for row in affected:
 name=row['name']
 for suffix in ['.config.json','.json','.stderr']:shutil.move(str(stage/(name+suffix)),str(old_cases/(name+suffix)))
 cfg=json.loads((old_cases/(name+'.config.json')).read_text());inp=stage/(name+'.config.json');dest=stage/(name+'.json');inp.write_text(json.dumps(cfg)+'\n')
 r=subprocess.run(['taskset','-c','8',str(OUTPUT_ROOT/'build/release/candidate_concurrent'),str(inp),str(dest)],capture_output=True,text=True,timeout=120);(stage/(name+'.stderr')).write_text(r.stderr);assert r.returncode==0,r.stderr
 d=json.loads(dest.read_text());assert d['allocation_bytes']==d['completion_bytes'] and d['quota_leaks']==0 and all(x['reserved']==x['posted']==0 for x in d['new_policy_stats'])
 lat=d['request_latency_ns'][1024:];duration=3072*cfg['arrival_interval_ns']/1e9
 row.update(p99_us=quantile(lat,.99)/1000,goodput_gbps=sum(d['measurement_bytes'])*8/duration/1e9,allocation_step=d['client_allocation_step'],allocation_wall_ns=d['allocation_ns']/d['allocation_calls'],stats=d['new_policy_stats']);rows.append(row)
 if row['policy']=='old_remainder' and row['workers']==8 and row['burst']:assert all(x['share0']==.5 for x in d['decisions'])
 new_cases.append({'name':name,'program':'candidate_concurrent','config_sha256':hashlib.sha256(inp.read_bytes()).hexdigest(),'result_sha256':hashlib.sha256(dest.read_bytes()).hexdigest()})
 if len(rows)%25==0:print('CONCURRENT_RECHECK',len(rows)-650,'/175',flush=True)
# Revalidate all five legacy thread traces against the previous E implementation.
for seed in [1,2,19,12345,4294967295]:
 inp=run/'validate'/('new_concurrent_'+str(seed)+'.config.json');dest=correction/('legacy-parity-'+str(seed)+'.json')
 subprocess.run([str(OUTPUT_ROOT/'build/release/candidate_concurrent'),str(inp),str(dest)],check=True,timeout=120)
 previous=json.loads((run/'validate'/('old_concurrent_'+str(seed)+'.json')).read_text());current=json.loads(dest.read_text());assert previous['request_latency_ns']==current['request_latency_ns']
summary['rows']=rows;summary['build']=after
(stage/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');(stage/'cases.json').write_text(json.dumps(new_cases,indent=2)+'\n')
# Real experiments used an identical binary; retain their measurements and
# attach the identical-binary proof when updating the build inventory.
r=json.loads((run/'real/summary.json').read_text());r['build']=after;(run/'real/summary.json').write_text(json.dumps(r,indent=2)+'\n')
proof.update(concurrent_cases_repeated=175,legacy_thread_parities=5,old_remainder_balanced_assertions=5)
(correction/'proof.json').write_text(json.dumps(proof,indent=2)+'\n');print('CONCURRENT_CONTROL_FIXED',proof,flush=True)
