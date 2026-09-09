#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT, REPO_ROOT
import pathlib,json,hashlib,subprocess
ROOT=pathlib.Path(__file__).resolve().parents[1];run=OUTPUT_ROOT/'runs/experiment-ef'
counts={}
for stage in ['validation','E','F','sanitize','real','replay']:
 cases=json.loads((run/stage/'cases.json').read_text());assert len(cases)==len({c['name'] for c in cases})
 for c in cases:
  for suffix,key in [('.config.json','config_sha256'),('.json','output_sha256')]:assert hashlib.sha256((run/stage/(c['name']+suffix)).read_bytes()).hexdigest()==c[key],(stage,c['name'])
 counts[stage]=len(cases)
for stage in ['E','F']:
 d=json.loads((run/stage/'summary.json').read_text());assert len(d['rows'])=={'E':560,'F':380}[stage]
 assert hashlib.sha256((ROOT/'scripts/run_experiment_ef.py').read_bytes()).hexdigest()==d['runner_sha256']
 for name,value in d['build']['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==value,name
 for name,value in d['build']['binaries'].items():assert hashlib.sha256((OUTPUT_ROOT/'build/release'/name).read_bytes()).hexdigest()==value,name
 assert all(c['passed'] for c in d['checks'])
for stage in ['validation','sanitize']:assert all(c['passed'] for c in json.loads((run/stage/'checks.json').read_text()))
real=json.loads((run/'real/summary.json').read_text());assert len(real['rows'])==75
for row in real['rows']:
 d=json.loads((run/'real'/(row['name']+'.json')).read_text());assert all(s['inflight']==0 for s in d['final_stats'])
 assert d['completion_count']==d['config']['requests']*16 and sum(d['completion_bytes'])==d['config']['requests']*1048576
 assert d['verified_final_slot_bytes']>0
assert hashlib.sha256((OUTPUT_ROOT/'build/release/real_rdma').read_bytes()).hexdigest()==real['build']['binary_sha256']
assert hashlib.sha256((ROOT/'simulator/driver_real.cpp').read_bytes()).hexdigest()==real['build']['source_sha256']
hw=json.loads((run/'hardware/summary.json').read_text());gpu=json.loads((run/'gpu/summary.json').read_text())
assert all(p['payload_verified'] for p in hw['pairs']) and all(p['payload_verified'] for p in gpu['pairs'])
for name,summary in [('cm_probe',hw),('cm_gpu',gpu)]:assert hashlib.sha256((OUTPUT_ROOT/'build/release'/name).read_bytes()).hexdigest()==summary['binary_sha256']
subprocess.run(['git','diff','--quiet','1c65ced88e509440e31ea88a76594660461d0eab','--','mooncake-transfer-engine/tent'],cwd=REPO_ROOT,check=True)
subprocess.run(['git','diff','--quiet','5f383e8e','--',str(ROOT/'simulator/driver.cpp'),str(ROOT/'simulator/driver_d.cpp')],cwd=REPO_ROOT,check=True)
regression=json.loads((run/'p01-regression/summary.json').read_text());assert regression['check_count']==30 and all(c['passed'] for c in regression['checks'])
comparator=json.loads((run/'comparator/result.json').read_text());assert comparator['strict_weak_order_violation']
result={'comparator_counterexample_verified':True,'p01_checks':30,'case_pairs':counts,'simulation_matrix_cases':940,'real_main_cases':75,'DRAM_CM_pairs':len(hw['pairs']),'GPU_CM_pairs':len(gpu['pairs']),'production_and_common_drivers_unchanged':True,'all_hashes_passed':True,'no_cross_node_endpoint_provided':True}
(run/'audit.json').write_text(json.dumps(result,indent=2)+'\n');print('EF_AUDIT_OK',result,flush=True)
