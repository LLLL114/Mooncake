#!/usr/bin/env python3
"""Repeat the two observed slow original-policy inputs; keep initial failures."""

import sys as _sys
_sys.dont_write_bytecode = True
from pathlib import Path as _BootstrapPath
_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1] / "scripts"))
from experiment_paths import OUTPUT_ROOT
import gzip,hashlib,json,pathlib,random,shutil,subprocess
from metrics import summarize
HERE=pathlib.Path(__file__).resolve().parent;ROOT=HERE.parent;RUN=OUTPUT_ROOT/'runs/acceptance-baseline';OUT=RUN/'repeat-slow'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def environment():
 result={}
 for name in ['/proc/stat','/proc/loadavg','/proc/net/dev','/proc/interrupts']:
  try:result[name]=pathlib.Path(name).read_text()
  except OSError as e:result[name]=str(e)
 return result
def main():
 OUT.mkdir(parents=True,exist_ok=True);source=json.loads((RUN/'real/rows.json').read_text());caps=json.loads((RUN/'real/calibration.json').read_text())['capacities'];rows=[];index=[]
 # Selection is explicit and prospective: repeat every steady input with a >10ms P99.
 slow=[r for r in source if r['kind']=='steady' and r['p99_us']>10000]
 jobs=[(r,k) for r in slow for k in range(1,6)];random.Random(91005).shuffle(jobs)
 for original,iteration in jobs:
  name=original['name']+'_repeat'+str(iteration);cfg=json.loads((RUN/'real'/(original['name']+'.config.json')).read_text());cfg.update(kind='diagnostic_repeat',repeat_of=original['name'],repeat_index=iteration)
  c=OUT/(name+'.config.json');p=OUT/(name+'.json');assert not c.exists() and not p.exists();c.write_text(json.dumps(cfg)+'\n');before=environment()
  with (OUT/(name+'.stderr')).open('w') as err:r=subprocess.run(['taskset','-c','8',str(OUTPUT_ROOT/'build/release/acceptance_real'),str(c),str(p)],stdout=err,stderr=err,timeout=180)
  assert r.returncode==0,name;after=environment();(OUT/(name+'.environment.json')).write_text(json.dumps({'before':before,'after':after})+'\n')
  d=json.loads(p.read_text());assert sum(d['completion_bytes'])==d['requests_completed']*cfg['request_bytes'] and all(x['inflight']==0 for x in d['final_stats'])
  row,win=summarize(d,d['raw_trace'],caps[str(cfg['request_bytes'])]);row.update(name=name,repeat_of=original['name'],iteration=iteration);rows.append(row)
  (OUT/(name+'.windows.json')).write_text(json.dumps(win)+'\n');raw=pathlib.Path(d['raw_trace']);rawhash=sha(raw);gz=pathlib.Path(str(raw)+'.gz')
  with raw.open('rb') as a,gzip.open(gz,'wb',compresslevel=1) as b:shutil.copyfileobj(a,b,1024*1024)
  raw.unlink();index.append({'name':name,'program':'acceptance_real','config_sha256':sha(c),'output_sha256':sha(p),'raw_sha256':rawhash,'raw_gzip_sha256':sha(gz)})
  (OUT/'rows.json').write_text(json.dumps(rows,indent=2,allow_nan=False)+'\n');(OUT/'index.json').write_text(json.dumps(index,indent=2)+'\n')
  print('REPEAT',len(rows),'/',len(jobs),name,round(row['goodput_gbps'],3),round(row['p99_us'],3),flush=True)
 print('REPEAT_DONE',len(rows),flush=True)
if __name__=='__main__':main()
