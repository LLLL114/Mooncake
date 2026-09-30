#!/usr/bin/env python3
"""Post-measurement attribution clues only; never exclude tails from primary P99."""
import bisect
from collections import Counter
import json
import math
from pathlib import Path
import statistics
from oscillation_metrics import load_trace, read_json, quantile
from run_tail_gain import ROOT


def main():
    target=ROOT/'tail-attribution.json'
    if target.exists():
        raise ValueError('existing diagnostics must not be overwritten')
    report=read_json(ROOT/'formal-analysis.json'); result=[]
    for row in report['runs']:
        root=Path(row['run_path']); m=read_json(root/'manifest.json')
        req=sorted((json.loads(s) for s in (root/'requests.jsonl').read_text().splitlines()),key=lambda r:r['submitted_ns'])
        times=[r['submitted_ns'] for r in req]; _,trace=load_trace(root); modes={}
        for r in trace:
            k=bisect.bisect_right(times,r['ns'])-1
            if k<0 or req[k]['finished_ns']<r['ns'] or req[k]['request_id'] in modes:
                raise ValueError('invalid request association')
            modes[req[k]['request_id']]='probe' if r['mode']==2 else 'normal_single' if min(r['assigned0'],r['assigned1'])==0 else 'normal_dual'
        cohort=[r for r in req if m['measurement_start_ns']<=r['planned_ns']<m['measurement_end_ns']]
        tail=sorted(cohort,key=lambda r:r['finished_ns']-r['planned_ns'])[-math.ceil(len(cohort)*.01):]
        get_mode=lambda r:modes.get(r['request_id'],'unobserved_boundary')
        groups={}
        for mode in ('probe','normal_single','normal_dual','unobserved_boundary'):
            records=[r for r in cohort if get_mode(r)==mode]
            groups[mode]=dict(count=len(records),p99_ms=quantile([(r['finished_ns']-r['planned_ns'])/1e6 for r in records],.99))
        o=read_json(root/'observer.json'); samples=[r for t in o['threads'] for r in t['rows'] if r['kind']=='release_sample']
        item=dict(variant=row['variant'],profile=row['profile'],repeat=row['repeat'],
            cohort=len(cohort),tail=len(tail),tail_modes=dict(Counter(get_mode(r) for r in tail)),groups=groups,
            post_to_cq_sample_mean_ms=1000*sum(r['values'][0]['sum'] for r in samples)/sum(r['n'] for r in samples),
            post_to_cq_sample_max_ms=1000*max(r['values'][0]['max'] for r in samples))
        result.append(item)
    summary=[]
    for variant in ('reference','integer','gain'):
        for profile in ('20pct','60pct'):
            a=[r for r in result if r['variant']==variant and r['profile']==profile]
            s=dict(variant=variant,profile=profile,
                probe_tail_fractions=[r['tail_modes'].get('probe',0)/r['tail'] for r in a],
                probe_cohort_fractions=[r['groups']['probe']['count']/r['cohort'] for r in a],
                unobserved_boundary=[r['groups']['unobserved_boundary']['count'] for r in a],
                post_to_cq_max_ms=[r['post_to_cq_sample_max_ms'] for r in a],
                post_to_cq_mean_ms=statistics.median(r['post_to_cq_sample_mean_ms'] for r in a))
            summary.append(s); print('TG_TAIL_GROUP',json.dumps(s),flush=True)
    target.write_text(json.dumps(dict(runs=result,groups=summary,
        caveat='conditional association, not causation; CQ time includes software polling delay; primary P99 unchanged'),indent=2))
    print('TG_TAIL_DIAGNOSTICS_COMPLETE',flush=True)


if __name__=='__main__':
    main()
