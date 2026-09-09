#!/usr/bin/env python3
"""Verify saved artifacts, exact build inputs, and unchanged production code."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT, REPO_ROOT
import hashlib
import csv
import json
import math
import pathlib
import subprocess

ROOT=pathlib.Path(__file__).resolve().parents[1]

def main():
    run=OUTPUT_ROOT/'runs/experiment-d/run'
    summary=json.loads((run/'summary.json').read_text())
    cases=json.loads((run/'case-manifest.json').read_text())
    assert len(cases)==summary['case_count'] and len({c['name'] for c in cases})==len(cases)
    assert summary['matrix_count']==len(summary['rows'])==405
    for case in cases:
        for suffix,key in [('.config.json','input_sha256'),('.json','result_sha256')]:
            assert hashlib.sha256((run/(case['name']+suffix)).read_bytes()).hexdigest()==case[key]
    for name,value in summary['source_sha256'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==value,name
    for name,value in summary['binary_sha256'].items():
        assert hashlib.sha256((OUTPUT_ROOT/'build/release'/name).read_bytes()).hexdigest()==value,name
    repo=REPO_ROOT
    for name,value in summary['build']['source_sha256'].items():
        assert hashlib.sha256((repo/name).read_bytes()).hexdigest()==value,name
    subprocess.run(['git','diff','--quiet','1c65ced88e509440e31ea88a76594660461d0eab','--','mooncake-transfer-engine/tent'],cwd=repo,check=True)
    # The common harness is unchanged from C, not just the transport sources.
    subprocess.run(['git','diff','--quiet','3ed7a094','--',str(ROOT/'simulator/driver.cpp'),str(ROOT/'scripts/build.py')],cwd=repo,check=True)
    # Independent arithmetic oracle for every observed static input, including
    # odd slice counts and candidates outnumbering slices.
    for fixture in summary['fixtures']:
        n=fixture['slice_count']; rails=fixture['rails']
        p=fixture['first_weights'][:rails]
        expected=[math.floor(n*x) for x in p]
        remaining=n-sum(expected)
        if fixture['mode']=='original':
            expected[max(range(rails),key=lambda i:p[i])]+=remaining
        else:
            order=sorted(range(rails),key=lambda i:(-(n*p[i]-math.floor(n*p[i])),i))
            for i in order[:remaining]:expected[i]+=1
        assert fixture['counts']==expected,fixture['name']
    for row in summary['rows']:
        assert row['deadline_exceeded']==0 and .999<row['goodput_gbps']/row['offered_gbps']<1.001
    historical=list(csv.DictReader((OUTPUT_ROOT/'reports/experiment-a-metrics.csv').open()))
    historical_pairs=0
    for row in summary['rows']:
        if row['phase']=='main' and row['mode']=='original':
            old=next(r for r in historical if r['topology']=='equal' and int(r['size'])==1048576
                     and float(r['load'])==row['load'] and int(r['seed'])==row['seed'] and r['strategy']=='smart')
            assert int(old['digest'])==row['digest'],row['name']
            assert abs(float(old['p99_us'])-row['p99_us'])<1e-9
            historical_pairs+=1
    assert historical_pairs==15
    result={'historical_A_summary_pairs':historical_pairs,'static_arithmetic_maps_verified':len(summary['fixtures']),'case_pairs':len(cases),'closed_loop_cases':len(summary['rows']),
            'production_and_common_harness_unchanged':True,'case_source_binary_hashes_verified':True,
            'throughput_ratio_range':[min(r['goodput_gbps']/r['offered_gbps'] for r in summary['rows']),
                                      max(r['goodput_gbps']/r['offered_gbps'] for r in summary['rows'])]}
    (run/'material-audit.json').write_text(json.dumps(result,indent=2)+'\n')
    print('D_AUDIT_OK',result,flush=True)

if __name__=='__main__':main()
