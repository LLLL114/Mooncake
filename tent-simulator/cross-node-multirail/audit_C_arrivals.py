#!/usr/bin/env python3
"""SSH-only check of actual planned timestamps, not just alignment flags."""
import json
from pathlib import Path
from summarize_requests import nearest_rank

BASE=Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')


def main():
    root=BASE/'followups-bc';p=json.loads((root/'progress-C-core.json').read_text())
    if p['complete'] is not True or len(p['outcomes'])!=50:raise ValueError('core incomplete')
    output=BASE/'reports/C-core-20260916/arrival-audit.json'
    if output.exists():raise ValueError('refusing to overwrite')
    rows=[]
    for item in p['outcomes']:
        state=json.loads((root/'C'/item['case_hash']/'state.json').read_text());args=state['case']['parameters']
        path=Path(item['run_path']);groups={};count=0
        with (path/'requests.jsonl').open() as stream:
            for line in stream:
                r=json.loads(line)
                if r['phase']!='measurement':continue
                count+=1;t=r['planned_ns'];submitted=r['submitted_ns'];bit=1<<r['flow_id']
                group=groups.setdefault(t,[0,submitted,submitted,0])
                if group[3]&bit:raise ValueError('same flow repeats a planned timestamp')
                group[0]+=1;group[1]=min(group[1],submitted);group[2]=max(group[2],submitted);group[3]|=bit
        times=sorted(groups);callers=args['callers'];alignment=args['arrival_alignment']
        if not times:raise ValueError('missing arrivals')
        if alignment=='synchronized':
            if any(groups[t][0]!=callers for t in times[:-1]) or not 1<=groups[times[-1]][0]<=callers:
                raise ValueError('synchronized planned groups differ from caller count')
            period=callers*args['size']*1e9/args['rate']
        else:
            if any(g[0]!=1 for g in groups.values()):raise ValueError('staggered planned timestamps overlap')
            period=args['size']*1e9/args['rate']
        gaps=[b-a for a,b in zip(times,times[1:])]
        if any(abs(g-period)>1.1 for g in gaps):raise ValueError('planned interval differs from fixed input budget')
        spreads=[g[2]-g[1] for g in groups.values() if g[0]==callers]
        rows.append(dict(case_hash=item['case_hash'],requests=count,callers=callers,alignment=alignment,
            planned_groups=len(groups),planned_period_ns=period,planned_gaps_ns=[min(gaps),max(gaps)],
            submit_group_spread_p99_ms=nearest_rank(spreads,990)/1e6 if spreads and callers>1 else None,
            window_per_caller=args['window']//callers,passed=True))
    with output.open('x') as f:json.dump(dict(scope='planned synchronization verified; actual submissions may spread due to queueing',
        limitation='total Q fixed but slots and waiting queues partitioned between callers',runs=rows),f,indent=2)
    print('C_ARRIVAL_AUDIT_COMPLETE',len(rows),sum(r['requests'] for r in rows))


if __name__=='__main__':main()
