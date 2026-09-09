#!/usr/bin/env python3
"""Verify byte/terminal conservation, raw evidence hashes, frozen production code."""

import sys as _sys
_sys.dont_write_bytecode = True
from pathlib import Path as _BootstrapPath
_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1] / "scripts"))
from experiment_paths import OUTPUT_ROOT, REPO_ROOT, ACCEPTANCE_OUTPUT, artifact_key, artifact_path
import gzip,hashlib,json,pathlib,subprocess
HERE=pathlib.Path(__file__).resolve().parent;ROOT=HERE.parent;REPO=REPO_ROOT;RUN=OUTPUT_ROOT/'runs/acceptance-baseline'
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for data in iter(lambda:f.read(1048576),b''):h.update(data)
 return h.hexdigest()
def main():
 checked={};evidence={};hashes={}
 for stage in ['validate','real','model','repeat-slow']:
  folder=RUN/stage;index=json.loads((folder/'index.json').read_text());checked[stage]=len(index)
  for item in index:
   name=item['name'];config=folder/(name+'.config.json');path=folder/(name+'.json');assert sha(config)==item['config_sha256']
   if path.exists():assert sha(path)==item['output_sha256'];d=json.loads(path.read_text())
   else:
    z=pathlib.Path(str(path)+'.gz');assert sha(z)==item['gzip_sha256'];raw=gzip.decompress(z.read_bytes());assert hashlib.sha256(raw).hexdigest()==item['output_sha256'];d=json.loads(raw)
   if 'raw_sha256' in item:
    raw=pathlib.Path(str(path)+'.requests.bin.gz');assert sha(raw)==item['raw_gzip_sha256'];h=hashlib.sha256();size=0
    with gzip.open(raw,'rb') as f:
     for data in iter(lambda:f.read(1048576),b''):h.update(data);size+=len(data)
    assert h.hexdigest()==item['raw_sha256'];assert size==56*d['requests_completed']
    assert d['completion_count']==d['requests_completed']*d['split_count'];assert sum(d['completion_bytes'])==d['requests_completed']*d['config']['request_bytes']
   if d.get('config',{}).get('kind')=='gate':
    assert item['program']=='controlled' and all(m['seen']>m['accepted']>0 and m['min_interval_ns']>=d['config']['update_interval_ns'] for m in d['sample_meters'])
   if 'allocation_bytes' in d:assert d['allocation_bytes']==d['completion_bytes']
   assert all(x['inflight']==0 for x in d.get('final_stats',[]))
   if 'quota_leaks' in d:assert d['quota_leaks']==0
  for p in folder.glob('*.json'):
   # Compact, tracked evidence: omit huge model per-request latency arrays;
   # full model files and raw real request records remain in runs/.
   hashes[artifact_key(p)]=sha(p)
   if stage!='validate':evidence[str(p.relative_to(RUN))]=json.loads(p.read_text())
  print('AUDIT',stage,len(index),flush=True)
 quota=REPO/'mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp'
 original=subprocess.check_output(['git','show','1c65ced88e509440e31ea88a76594660461d0eab:mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp'],cwd=REPO)
 assert hashlib.sha256(original).hexdigest()==sha(quota),'production selector changed'
 # Byte-identical derived model and original model follow the same algorithm.
 checks=json.loads((RUN/'validate/checks.json').read_text());assert len(checks)==21 and all(x['passed'] for x in checks)
 rows=json.loads((RUN/'real/rows.json').read_text());formal=[r for r in rows if r['kind'] in ['steady','single','saturated']]
 assert len(formal)==75
 assert all(r['p99_windows']['n']==40 and r['pending_at_end']==0 and r['quota_inflight_at_end']==0 for r in formal)
 correction=json.loads((ACCEPTANCE_OUTPUT/'gate-correction-manifest.json').read_text());assert len(correction['zero_gate_parity'])==3
 for name,digest in correction['hashes'].items():assert sha(artifact_path(name))==digest
 build=json.loads((ACCEPTANCE_OUTPUT/'build-manifest.json').read_text())
 for name,digest in build['binaries'].items():assert sha(OUTPUT_ROOT/'build/release'/name)==digest
 for name,digest in build['files'].items():assert sha(artifact_path(name))==digest
 with gzip.GzipFile(filename=str(ACCEPTANCE_OUTPUT/'baseline-evidence.json.gz'),mode='wb',mtime=0,compresslevel=6) as f:f.write(json.dumps(evidence,separators=(',',':'),allow_nan=False).encode())
 manifest={'case_counts':checked,'production_quota_sha256':sha(quota),'checks':len(checks),'formal_real_runs':len(formal),'all_formal_runs_40_valid_p99_windows':True,'data_sha256':hashes,'source_sha256':{p.name:sha(p) for p in HERE.iterdir() if p.is_file() and p.suffix in ['.py','.h','.cpp']},'compact_evidence_sha256':sha(ACCEPTANCE_OUTPUT/'baseline-evidence.json.gz'),'full_pipeline_faults':'not covered','flow_retry_migration':'not covered'}
 (ACCEPTANCE_OUTPUT/'audit-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');(ACCEPTANCE_OUTPUT/'audit-summary.md').write_text('# Baseline 数据核验\n\n'+json.dumps(checked)+'\n\n75 次正式实机均有 40 个有效 P99 窗口；完成/字节/配额守恒、原始记录和配置 SHA、源码/二进制 SHA 检查通过。生产 quota.cpp 与原始基线一致。完整故障状态机和真实迁移语义未覆盖。\n')
 print('BASELINE_AUDIT_OK',checked,flush=True)
if __name__=='__main__':main()
