#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import argparse,hashlib,json,pathlib,subprocess,difflib
ROOT=pathlib.Path(__file__).resolve().parents[1]
def main():
 p=argparse.ArgumentParser();p.add_argument('--sanitize',action='store_true');a=p.parse_args()
 b=OUTPUT_ROOT/'build'/('sanitize' if a.sanitize else 'release');manifest=json.loads((b/'manifest.json').read_text())
 command=next(c for c in manifest['commands'] if c[-1]==str(b/'observed'))
 source=(b/'quota.clock.cpp').read_text();anchor='    return Status::OK();\n}\n\nvoid DeviceSelector::selectSinglePath('
 assert source.count(anchor)==1
 source='#include "concurrency.h"\n'+source.replace(anchor,'    experiment::parallelCandidates(candidates);\n'+anchor)
 (b/'quota.e.cpp').write_text(source)
 common=(ROOT/'simulator/driver.cpp').read_text()
 old='    const uint64_t gap = config.value("arrival_interval_ns", 50000ULL);'
 assert common.count(old)==1
 changed=common.replace(old,old+'''
    auto request_times = config.value("request_arrivals_ns", std::vector<uint64_t>{});
    if (request_times.empty()) for(uint64_t r=0;r<requests;++r) request_times.push_back(r*gap);
    require(request_times.size()==requests && std::is_sorted(request_times.begin(),request_times.end()),"invalid arrival trace");''')
 changed=changed.replace('metric_start=config.value("warmup_requests",0ULL)*gap;', 'metric_start=config.value("measurement_start_ns",config.value("warmup_requests",0ULL)*gap);')
 changed=changed.replace('metric_stop=requests*gap;', 'metric_stop=config.value("measurement_stop_ns",requests*gap);')
 changed=changed.replace('uint64_t arrival = req * gap;', 'uint64_t arrival = request_times[req];')
 assert changed!=common and changed.count('request_times[req]')==1
 (b/'driver_f_model.inc').write_text(changed)
 wrapper=(ROOT/'simulator/driver_d.cpp').read_text().replace('#include "driver.cpp"','#include "driver_f_model.inc"')
 (b/'driver_f.cpp').write_text(wrapper)
 commands=[]
 for name,driver,quota in [('concurrency',ROOT/'simulator/driver_e.cpp',b/'quota.e.cpp'),('dynamic',b/'driver_f.cpp',b/'quota.d.cpp')]:
  cmd=[str(driver) if c==str(ROOT/'simulator/driver.cpp') else str(quota) if c==str(b/'quota.observed.cpp') else str(b/name) if c==str(b/'observed') else c for c in command]
  subprocess.run(cmd,check=True);commands.append(cmd)
 (b/'dynamic-model.patch').write_text(''.join(difflib.unified_diff(common.splitlines(True),changed.splitlines(True),fromfile='driver.cpp',tofile='driver_f_model.inc')))
 files=['simulator/driver.cpp','simulator/driver_d.cpp','simulator/driver_e.cpp','simulator/concurrency.h','simulator/hooks.h','simulator/allocation.h','scripts/build_experiment_ef.py']
 (b/'manifest-ef.json').write_text(json.dumps({'commands':commands,'base':manifest,'sources':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files},'binaries':{n:hashlib.sha256((b/n).read_bytes()).hexdigest() for n in ['concurrency','dynamic']}},indent=2)+'\n')
 print('EF_BUILD_OK',flush=True)
if __name__=='__main__':main()
