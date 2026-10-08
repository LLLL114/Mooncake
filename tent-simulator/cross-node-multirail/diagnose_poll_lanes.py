#!/usr/bin/env python3
"""Read-only Rail/lane association, normalized by observed Slice count."""
from collections import Counter,defaultdict
import json
import math
from pathlib import Path
from oscillation_metrics import describe,read_json
from poll_diagnostic_metrics import load
from run_poll_diagnostic import ROOT


def stats(values):
    return dict(count=len(values),over10ms=sum(x>=10 for x in values),
        over100ms=sum(x>=100 for x in values),latency_ms=describe(values))


def main():
    output=ROOT/'rail-lane-diagnostic.json'
    if output.exists(): raise ValueError('existing diagnostic')
    report=read_json(ROOT/'formal-analysis.json');rows=[]
    for row in report['runs']:
        if row['variant']!='timed':continue
        root=Path(row['run_path']);m=read_json(root/'manifest.json');meta,records,_=load(root)
        requests=[json.loads(s) for s in (root/'requests.jsonl').read_text().splitlines()]
        requests=[r for r in requests if m['measurement_start_ns']<=r['planned_ns']<m['measurement_end_ns']]
        tail=sorted(requests,key=lambda r:r['finished_ns']-r['planned_ns'])[-math.ceil(len(requests)*.01):]
        tail_ids={r['request_id'] for r in tail};rail=defaultdict(list);lane=defaultdict(list);tail_critical=Counter()
        for r in requests:
            slices=records[r['request_id']]
            for s in slices:
                name=m['environment']['nic_id_to_name'][str(s['dev'])];delay=(s['poll_end']-s['submit'])/1e6
                rail[name].append(delay);lane[(name,s['worker'],s['qp'])].append(delay)
            if r['request_id'] in tail_ids:
                critical=max(slices,key=lambda s:(s['poll_end'],s['handled']))
                tail_critical[m['environment']['nic_id_to_name'][str(critical['dev'])]]+=1
        result=dict(profile=row['profile'],repeat=row['repeat'],clock_mapping=meta['monotonic_mapping'],
            rails={k:stats(v) for k,v in rail.items()},
            lanes=[dict(rail=k[0],worker=k[1],qp_index=k[2],**stats(v)) for k,v in sorted(lane.items())],
            tail_requests=len(tail),tail_critical_rail=dict(tail_critical))
        rows.append(result)
        print('PD_LANES',json.dumps(dict(profile=result['profile'],repeat=result['repeat'],
            clock_mapping=result['clock_mapping'],rails=result['rails'],tail_critical_rail=result['tail_critical_rail'])),flush=True)
    output.write_text(json.dumps(dict(runs=rows,caveat='assignment-dependent associations, not matched traffic or causal rail comparisons; qp_index is not hardware QPN'),indent=2))
    print('PD_LANE_DIAGNOSTIC_COMPLETE',flush=True)


if __name__=='__main__':main()
