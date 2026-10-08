#!/usr/bin/env python3
"""Offline analysis: actual provider calls, local QPNs, paired perturbation."""
import argparse
from collections import defaultdict
import json
import statistics
import analyze_poll_diagnostic as previous
from poll_diagnostic_metrics import load
from run_provider_diagnostic import ROOT,validate
import run_suite_collection_v2 as suite


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',choices=['pilot','overhead','formal'],required=True);args=p.parse_args()
    target=ROOT/(args.stage+'-analysis.json')
    if target.exists(): raise ValueError('existing analysis retained')
    progress=suite._read(ROOT/('progress-'+args.stage+'.json'))
    if not progress['complete']: raise ValueError('incomplete stage')
    cases={suite.case_hash(c):c for c in suite._read(ROOT/('plan-'+args.stage+'.json'))['cases']}
    # Reuse the tested time-chain calculation, selecting this experiment's validator/output.
    previous.ROOT=ROOT;previous.validate=validate
    rows=[]
    for item in progress['outcomes']:
        case=cases[item['case_hash']];row=previous.one(case,item);row['mode']=case['mode']
        if case['diagnostic']:
            root=previous.Path(item['run_path']);meta,records,gaps=load(root)
            manifest=suite._read(root/'manifest.json')
            requests=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            measured={q['request_id'] for q in requests if manifest['measurement_start_ns']<=q['planned_ns']<manifest['measurement_end_ns']}
            buckets=defaultdict(lambda:dict(slices=0,slow10ms=0,slow100ms=0))
            for epoch,slices in records.items():
                if epoch not in measured: continue
                for s in slices:
                    key=(s['dev'],s['hardware_qpn'],s['worker'],s['qp'])
                    b=buckets[key];b['slices']+=1
                    elapsed=s['poll_end']-s['submit'];b['slow10ms']+=elapsed>=10000000;b['slow100ms']+=elapsed>=100000000
            row['qps']=[dict(dev=k[0],qpn=k[1],worker=k[2],qp_index=k[3],**b) for k,b in sorted(buckets.items())]
            row['provider']=suite._read(root/'provider-summary.json')
        rows.append(row)
    refs={(r['mode'],r['profile'],r['repeat']):r for r in rows if r['variant']=='reference'}
    pairs=[]
    for r in rows:
        if r['variant']!='provider': continue
        ref=refs[(r['mode'],r['profile'],r['repeat'])]
        pairs.append(dict(mode=r['mode'],profile=r['profile'],repeat=r['repeat'],
            goodput_ratio=r['performance']['goodput_gbps']/ref['performance']['goodput_gbps'],
            p99_ratio=r['performance']['p99_ms']/ref['performance']['p99_ms']))
    result=dict(stage=args.stage,runs=rows,pairs=pairs,
        caveats=['Provider entry is proven; timing encloses wrapper+verbs, not hardware completion.',
                 'QPNs are local per-device/per-run identifiers; do not compare numeric QPNs across runs.',
                 'Matched offered load is not matched utilization. Single Rail may approach capacity.',
                 'Diagnostic/reference P99 differences are observational perturbation, not algorithm gains.'])
    if args.stage=='overhead':
        result['gate_pass']=statistics.median(p['goodput_ratio'] for p in pairs)>=.95 and statistics.median(p['p99_ratio'] for p in pairs)<=1.20
    suite._save(target,result,exclusive=True)
    print('PV_ANALYSIS_COMPLETE',args.stage,result.get('gate_pass'),json.dumps(pairs),flush=True)


if __name__=='__main__': main()
