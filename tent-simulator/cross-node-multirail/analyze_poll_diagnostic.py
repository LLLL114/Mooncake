#!/usr/bin/env python3
"""Offline paired overhead and critical-Slice/poll-service attribution."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics
from oscillation_metrics import describe,read_json
from poll_diagnostic_metrics import load,overlap_index,timing
from run_poll_diagnostic import ROOT,validate
import run_suite_collection_v2 as suite
from summarize_baseline import CORE,WEIGHTS,request_metrics


def one(case,item):
    root=Path(item['run_path']);validate(case,dict(runPath=str(root),status=item['status']))
    m,s,w=[read_json(root/(n+'.json')) for n in ('manifest','summary','windows')]
    metrics=dict.fromkeys(CORE+WEIGHTS);request_metrics(m,s,w,metrics,{})
    row=dict(case_hash=item['case_hash'],run_path=str(root),variant=case['variant'],profile=case['load'],repeat=case['repeat'],
        performance={k:metrics[k] for k in CORE},counts=m['total'])
    if not case['diagnostic']: return row
    meta,records,gaps=load(root);index=overlap_index(gaps)
    req=[json.loads(s) for s in (root/'requests.jsonl').read_text().splitlines()]
    req=[r for r in req if m['measurement_start_ns']<=r['planned_ns']<m['measurement_end_ns']]
    worst=sorted(req,key=lambda r:r['finished_ns']-r['planned_ns'])[-max(1,math.ceil(len(req)*.01)):]
    tail_ids={r['request_id'] for r in worst};parts=[];slow=[];tail_slow=[];details=[]
    for q in req:
        slices=records[q['request_id']]
        critical=max(slices,key=lambda s:(s['poll_end'],s['handled']))
        segments=[q['submitted_ns']-q['planned_ns'],critical['enqueue']-q['submitted_ns'],
            critical['submit']-critical['enqueue'],critical['poll_end']-critical['submit'],q['finished_ns']-critical['poll_end']]
        if sum(segments)!=q['finished_ns']-q['planned_ns'] or min(segments)<0: raise ValueError('critical-path time identity failed')
        current=dict(request=q['request_id'],e2e_ms=(q['finished_ns']-q['planned_ns'])/1e6,
            parts_ms=[x/1e6 for x in segments],critical_timing=timing(critical,index),
            observation_delay_lower_ms=max(0,q['finished_ns']-max(s['handled'] for s in slices))/1e6,
            observation_delay_upper_ms=(q['finished_ns']-max(s['poll_end'] for s in slices))/1e6)
        if q['request_id'] in tail_ids:
            parts.append(current);details.append(current)
        for s in slices:
            if s['poll_end']-s['submit']>=10000000:
                sample=timing(s,index);slow.append(sample)
                if q['request_id'] in tail_ids: tail_slow.append(sample)
    names=['injection','submit_to_enqueue','enqueue_to_post_mark','post_mark_to_cq','cq_to_caller']
    tail=dict(requests=len(parts),part_ms={n:describe([r['parts_ms'][i] for r in parts]) for i,n in enumerate(names)},
        part_fraction={n:describe([r['parts_ms'][i]/r['e2e_ms'] for r in parts]) for i,n in enumerate(names)},
        caller_delay_lower_ms=describe([r['observation_delay_lower_ms'] for r in parts]),
        caller_delay_upper_ms=describe([r['observation_delay_upper_ms'] for r in parts]))
    def slow_summary(values):
        return dict(slices=len(values),categories=dict(Counter(x['category'] for x in values)),
            gap_fraction=describe([x['gap_fraction'] for x in values]),
            current_poll_ms=describe([x['current_poll_ms'] for x in values]),
            post_cq_ms=describe([x['post_cq_ms'] for x in values]))
    row.update(tail=tail,slow_slices=slow_summary(slow),tail_slow_slices=slow_summary(tail_slow))
    path=ROOT/'analysis-details';path.mkdir(exist_ok=True)
    suite._save(path/(item['case_hash']+'.json'),dict(tail_requests=details,metadata=meta),exclusive=True)
    return row


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',choices=['overhead','formal'],required=True);args=p.parse_args()
    target=ROOT/(args.stage+'-analysis.json')
    if target.exists(): raise ValueError('existing analysis')
    progress=read_json(ROOT/('progress-'+args.stage+'.json'))
    if not progress['complete']: raise ValueError('measurement incomplete')
    cases={suite.case_hash(c):c for c in read_json(ROOT/('plan-'+args.stage+'.json'))['cases']}
    rows=[one(cases[item['case_hash']],item) for item in progress['outcomes']]
    refs={(r['profile'],r['repeat']):r for r in rows if r['variant']=='reference'}
    pairs=[]
    for r in rows:
        if r['variant']!='timed':continue
        base=refs[(r['profile'],r['repeat'])]
        pairs.append(dict(profile=r['profile'],repeat=r['repeat'],
            goodput_ratio=r['performance']['goodput_gbps']/base['performance']['goodput_gbps'],
            p99_ratio=r['performance']['p99_ms']/base['performance']['p99_ms']))
    result=dict(stage=args.stage,runs=rows,pairs=pairs,
        caveats=['no hardware completion timestamp; software polls cannot uniquely identify network/device/CPU causes',
            'handled timestamp is after status publication, so caller delay is bounded, not measured exactly',
            'critical path uses latest CQ completion, ties by handling end; not a unique causal path'])
    if args.stage=='overhead':
        result['gate_pass']=statistics.median(p['goodput_ratio'] for p in pairs)>=.95 and statistics.median(p['p99_ratio'] for p in pairs)<=1.20
    suite._save(target,result,exclusive=True)
    print('PD_ANALYSIS_COMPLETE',args.stage,result.get('gate_pass'),json.dumps(pairs),flush=True)


if __name__=='__main__':main()
