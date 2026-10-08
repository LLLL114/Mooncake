#!/usr/bin/env python3
"""Intersect existing coarse CQ-quota samples with known posted Slice lifetimes."""
from collections import defaultdict
import json
from pathlib import Path
from oscillation_metrics import read_json
from poll_diagnostic_metrics import load
from run_poll_diagnostic import ROOT


def main():
    output=ROOT/'poll-gate-samples.json'
    if output.exists(): raise ValueError('existing gate analysis')
    report=read_json(ROOT/'formal-analysis.json'); results=[]
    for row in report['runs']:
        if row['variant']!='timed': continue
        root=Path(row['run_path']);m=read_json(root/'manifest.json');o=read_json(root/'observer.json')
        meta,records,_=load(root);samples=defaultdict(list)
        for thread in o['threads']:
            contexts={c['id']:c for c in thread['contexts']}
            for s in thread['rows']:
                if s['kind']=='cq_queue':
                    samples[(contexts[s['context']]['worker'],s['dev'],s['bin'])].append(s['values'][0])
        req=[json.loads(s) for s in (root/'requests.jsonl').read_text().splitlines()]
        ids={r['request_id'] for r in req if m['measurement_start_ns']<=r['planned_ns']<m['measurement_end_ns']}
        counts=dict(long_slices=0,with_interior_samples=0,without_samples=0,with_zero_sample=0,with_negative_sample=0,all_samples_positive=0)
        unique={}; margin=1000000+meta['monotonic_mapping']['uncertainty_ns']
        for epoch in ids:
            for s in records[epoch]:
                if s['poll_end']-s['submit']<100000000: continue
                counts['long_slices']+=1
                first=(s['submit']+margin-m['measurement_start_ns']+49999999)//50000000
                stop=(s['poll_end']-margin-m['measurement_start_ns'])//50000000
                values=[]
                for b in range(max(0,first),stop):
                    key=(s['worker'],s['dev'],b)
                    if key in samples: values.extend(samples[key]);unique[key]=samples[key]
                if not values: counts['without_samples']+=1;continue
                counts['with_interior_samples']+=1
                counts['with_zero_sample']+=any(v['min']==0 for v in values)
                counts['with_negative_sample']+=any(v['min']<0 for v in values)
                counts['all_samples_positive']+=all(v['min']>0 for v in values)
        counts['unique_interior_bins']=len(unique)
        counts['unique_bins_with_zero']=sum(any(v['min']==0 for v in values) for values in unique.values())
        result=dict(profile=row['profile'],repeat=row['repeat'],counts=counts);results.append(result)
        print('PD_GATE_SAMPLES',json.dumps(result),flush=True)
    output.write_text(json.dumps(dict(runs=results,
        interpretation='50ms bucket samples with whole-bucket lifetime containment plus 1ms margin; not per-call provider-entry evidence; Slice counts reuse shared CQ samples'),indent=2))
    print('PD_GATE_SAMPLE_COMPLETE',flush=True)


if __name__=='__main__':main()
