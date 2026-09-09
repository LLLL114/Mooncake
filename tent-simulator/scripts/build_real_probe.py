#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import pathlib,json,subprocess,hashlib
ROOT=pathlib.Path(__file__).resolve().parents[1]
b=OUTPUT_ROOT/'build/release'
s=(ROOT/'simulator/driver_d.cpp').read_text();(b/'driver_d_shared.inc').write_text(s[:s.index('int main(int argc, char** argv)')])
m=json.loads((b/'manifest.json').read_text());c=next(c for c in m['commands'] if c[-1]==str(b/'observed'))
c=[str(ROOT/'simulator/driver_real.cpp') if x==str(ROOT/'simulator/driver.cpp') else str(b/'quota.d.cpp') if x==str(b/'quota.observed.cpp') else str(b/'real_rdma') if x==str(b/'observed') else x for x in c]+['-lrdmacm','-libverbs']
subprocess.run(c,check=True)
(b/'manifest-real.json').write_text(json.dumps({'command':c,'source_sha256':hashlib.sha256((ROOT/'simulator/driver_real.cpp').read_bytes()).hexdigest(),'binary_sha256':hashlib.sha256((b/'real_rdma').read_bytes()).hexdigest()},indent=2)+'\n')
print('REAL_BUILD_OK',flush=True)
