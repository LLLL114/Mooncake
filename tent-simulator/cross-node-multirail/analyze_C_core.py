#!/usr/bin/env python3
"""SSH-only fixed-resource caller comparison; matched by load and repeat."""
from collections import defaultdict
import json
from pathlib import Path

from summarize_baseline import CORE, WEIGHTS, interval, request_metrics, observer_metrics
from run_followups_bc import validate

BASE=Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')
COMPARE=('goodput_gbps','p99_ms','throughput_250ms_cv','p99_window_cv',
         'allocation_250ms_share_sd_erdma_0','consumed_changes_0.001_per_second',
         'consumed_changes_per_10000_comparisons')


def main():
    root=BASE/'followups-bc';progress=json.loads((root/'progress-C-core.json').read_text())
    if progress['complete'] is not True or len(progress['outcomes'])!=50:raise ValueError('C core incomplete')
    output=BASE/'reports/C-core-20260916';output.mkdir(parents=True,exist_ok=True)
    if (output/'summary.json').exists():raise ValueError('refusing to overwrite')
    cap=json.loads((BASE/'suites-extended/capacity.json').read_text());rows=[]
    for item in progress['outcomes']:
        state=json.loads((root/'C'/item['case_hash']/'state.json').read_text());case=state['case'];last=state['attempts'][-1]
        if last['status']!='success' or case['parameters']['workers'] is not None:raise ValueError('not a successful core case')
        validate(case,last)
        run=Path(last['runPath']);read=lambda name:json.loads((run/(name+'.json')).read_text())
        m,s,w,o=map(read,('manifest','summary','windows','observer'))
        metrics=dict.fromkeys(CORE+WEIGHTS);evidence={}
        request_metrics(m,s,w,metrics,evidence);observer_metrics(o,m,cap,metrics,evidence)
        if evidence.get('observer_complete') is not True or evidence.get('analysis_loss'):raise ValueError('incomplete observer')
        counts=m['total']
        if counts['accepted']!=counts['success'] or counts['failure'] or counts['pending'] or m['data_verified'] is not True:
            raise ValueError('run not correctly completed')
        comparisons=metrics.get('consumed_comparisons')
        metrics['consumed_changes_per_10000_comparisons']=(metrics['consumed_changes_0.001_per_second']*case['parameters']['seconds']*10000/comparisons) if comparisons else None
        posts=defaultdict(int)
        for thread in o['threads']:
            contexts={c['id']:c for c in thread['contexts']}
            for row in thread['rows']:
                if row['kind']=='post' and row['bytes']:
                    worker=contexts[row['context']]['worker']
                    posts[f"{row['dev']}:{worker}"]+=row['bytes']
        total=sum(posts.values())
        record=dict(case_hash=item['case_hash'],run_path=str(run),repeat=case['repeat'],
                    load=round(case['parameters']['rate']/case['capacity_reference']['d0_bytes_per_second']*100),
                    callers=case['parameters']['callers'],alignment=case['parameters']['arrival_alignment'],
                    counts=counts,metrics=metrics,post_bytes_by_nic_worker=dict(posts),
                    post_share_by_nic_worker={k:v/total for k,v in posts.items()} if total else {},
                    post_worker_ids=sorted({int(k.split(':')[1]) for k in posts}),
                    verified=True,observer_complete=True)
        rows.append(record)
        print('C_ANALYZED',len(rows),flush=True)
    controls={(r['load'],r['repeat']):r for r in rows if r['callers']==1}
    buckets=defaultdict(list);pairs=defaultdict(list)
    for row in rows:
        key=(row['load'],row['callers'],row['alignment']);buckets[key].append(row)
        if row['callers']!=1:
            control=controls[(row['load'],row['repeat'])]
            pairs[key].append({k:100*(row['metrics'][k]/control['metrics'][k]-1)
                               if type(row['metrics'].get(k)) in (int,float) and type(control['metrics'].get(k)) in (int,float)
                               and control['metrics'][k]!=0 else None for k in COMPARE})
    groups=[]
    for key,items in sorted(buckets.items()):
        if len(items)!=5 or {r['repeat'] for r in items}!={1,2,3,4,5}:raise ValueError('missing paired repetitions')
        groups.append(dict(load=key[0],callers=key[1],alignment=key[2],runs=5,
            metrics={k:interval([r['metrics'].get(k) for r in items]) for k in COMPARE},
            paired_change_pct={k:interval([p[k] for p in pairs[key]]) for k in COMPARE} if key in pairs else None,
            active_post_workers=sorted({tuple(r['post_worker_ids']) for r in items})))
    report=dict(scope='C core only; fixed aggregate input/Q and configured QP resources; no lane=1 controls',
        limitations=['fixed offered-load goodput is not a saturated-capacity test',
                     'caller count also changes producer CPU/concurrency and potentially actual worker/QP use',
                     'post worker IDs are observed dispatch labels, not unique QP identities',
                     'five paired repetitions; descriptive ranges, no claim of statistical significance'],
        total_requests=sum(r['counts']['accepted'] for r in rows),failure=0,pending=0,
        retry_events=sum(r['metrics']['retry_events'] for r in rows),groups=groups,runs=rows)
    with (output/'summary.json').open('x') as stream:json.dump(report,stream,indent=2)
    print('C_CORE_ANALYSIS_COMPLETE',len(rows),str(output),flush=True)


if __name__=='__main__':main()
