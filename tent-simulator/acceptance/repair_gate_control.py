#!/usr/bin/env python3
"""One-time provenance-preserving repair. Clean runs use the fixed run_model.py."""

import sys as _sys
_sys.dont_write_bytecode = True
from pathlib import Path as _BootstrapPath
_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1] / "scripts"))
from experiment_paths import OUTPUT_ROOT, ACCEPTANCE_OUTPUT, artifact_key
import gzip,hashlib,json,pathlib,shutil,subprocess
HERE=pathlib.Path(__file__).resolve().parent;ROOT=HERE.parent;OUT=OUTPUT_ROOT/'runs/acceptance-baseline/model';ARCHIVE=OUT.parent/'model-gate-correction'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 assert not ARCHIVE.exists();ARCHIVE.mkdir()
 rows=json.loads((OUT/'rows.json').read_text());index=json.loads((OUT/'index.json').read_text());bad=[r['name'] for r in rows if r['kind']=='gate'];assert len(bad)==6
 for name in ['rows.json','index.json']:shutil.copy2(OUT/name,ARCHIVE/name)
 for name in bad:
  for p in OUT.glob(name+'.*'):shutil.move(str(p),str(ARCHIVE/p.name))
 (OUT/'rows.json').write_text(json.dumps([r for r in rows if r['name'] not in bad],indent=2)+'\n')
 (OUT/'index.json').write_text(json.dumps([r for r in index if r['name'] not in bad],indent=2)+'\n')
 checks=[]
 for r in rows:
  if not(r['kind']=='steady' and r['size']==1048576 and r['topology']=='equal' and r['load']==.9):continue
  cfg=OUT/(r['name']+'.config.json');dest=ARCHIVE/(str(r['seed'])+'.zero-gate.json')
  p=subprocess.run([str(OUTPUT_ROOT/'build/release/controlled'),str(cfg),str(dest)],capture_output=True,text=True,timeout=60);assert p.returncode==0,p.stderr
  before=json.loads(gzip.decompress((OUT/(r['name']+'.json.gz')).read_bytes()));after=json.loads(dest.read_text())
  assert before['digest']==after['digest'] and before['request_latency_ns']==after['request_latency_ns']
  checks.append({'seed':r['seed'],'config_sha256':sha(cfg),'zero_gate_result_sha256':sha(dest),'original_result_sha256':hashlib.sha256(gzip.decompress((OUT/(r['name']+'.json.gz')).read_bytes())).hexdigest(),'exact_digest_and_latency_parity':True})
 assert len(checks)==3
 subprocess.run(['python3',str(HERE/'run_model.py'),'--only-gate'],check=True)
 files=[OUTPUT_ROOT/'build/release/controlled',OUTPUT_ROOT/'build/release/quota.controlled.cpp',ROOT/'simulator/sampling.h',HERE/'run_model.py']
 manifest={'quarantined_cases':bad,'zero_gate_parity':checks,'hashes':{artifact_key(p):sha(p) for p in files},'scope':'Only six gated model cases are replaced. Real baseline and other model cases are retained.'}
 (ACCEPTANCE_OUTPUT/'gate-correction-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');print('GATE_CONTROL_REPAIRED',len(bad),len(checks),flush=True)
if __name__=='__main__':main()
