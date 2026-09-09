#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT, REPO_ROOT
import pathlib,json,hashlib,subprocess
ROOT=pathlib.Path(__file__).resolve().parents[1];run=OUTPUT_ROOT/'runs/candidate-algorithms';counts={}
for stage in ['validate','sanitize','sim','real']:
 cases=json.loads((run/stage/'cases.json').read_text());assert len(cases)==len({c['name'] for c in cases})
 for c in cases:
  for suffix,key in [('.config.json','config_sha256'),('.json','result_sha256')]:assert hashlib.sha256((run/stage/(c['name']+suffix)).read_bytes()).hexdigest()==c[key],(stage,c['name'])
 counts[stage]=len(cases)
 if stage!='real':assert all(c['passed'] for c in json.loads((run/stage/'checks.json').read_text()))
for stage,expected in [('sim',825),('real',215)]:
 d=json.loads((run/stage/'summary.json').read_text());assert len(d['rows'])==expected
 assert hashlib.sha256((ROOT/'scripts/run_candidates.py').read_bytes()).hexdigest()==d['runner_sha256']
 for name,value in d['build']['source_sha256'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==value,name
 for name,value in d['build']['binary_sha256'].items():assert hashlib.sha256((OUTPUT_ROOT/'build/release'/name).read_bytes()).hexdigest()==value,name
 for r in d['rows']:
  assert all(x['reserved']==0 and x['posted']==0 for x in r.get('stats',[])),r['name']
  if stage=='real':
   raw=json.loads((run/stage/(r['name']+'.json')).read_text());assert all(x['inflight']==0 for x in raw['final_stats'])
   assert raw['completion_count']==raw['config']['requests']*16 and raw['verified_final_slot_bytes']>0
   for device in raw['devices']:assert device['source_nic']==device['target_nic']=='erdma_'+str(device['rail'])
v1=OUTPUT_ROOT/'runs/candidate-algorithms-v1';old=json.loads((v1/'manifest-release.json').read_text())
for name,value in old['source_sha256'].items():assert hashlib.sha256((v1/'sources'/name).read_bytes()).hexdigest()==value,name
for name,value in old['binary_sha256'].items():assert hashlib.sha256((v1/'binaries'/name).read_bytes()).hexdigest()==value,name
subprocess.run(['git','diff','--quiet','1c65ced88e509440e31ea88a76594660461d0eab','--','mooncake-transfer-engine/tent'],cwd=REPO_ROOT,check=True)
proof=json.loads((run/'concurrent-control-correction/proof.json').read_text());assert proof['concurrent_cases_repeated']==175 and proof['old_remainder_balanced_assertions']==5
result={'concurrent_control_correction_verified':True,'case_pairs':counts,'sim_matrix':825,'real_cases':215,'production_unchanged':True,'hashes_verified':True}
(run/'audit.json').write_text(json.dumps(result,indent=2)+'\n');print('CANDIDATE_AUDIT_OK',result,flush=True)
