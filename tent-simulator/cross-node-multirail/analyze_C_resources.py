#!/usr/bin/env python3
"""SSH-only matched-lane resource audit; no original A data is modified."""
from collections import defaultdict
import json
from pathlib import Path

from audit_baseline import audit
from summarize_baseline import CORE, WEIGHTS, interval, request_metrics, observer_metrics
from run_C_resources import check_peer_result

BASE=Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')


def main():
    root=BASE/'followups-c-resources';progress=json.loads((root/'progress-formal.json').read_text())
    if not progress['complete'] or len(progress['outcomes'])!=10:raise ValueError('resource collection incomplete')
    output=BASE/'reports/C-resources-20260916';output.mkdir(parents=True,exist_ok=True)
    if (output/'summary.json').exists():raise ValueError('refusing to overwrite')
    rows=[];audits=[]
    for phase,directory in [('pilot','C-resource-pilot'),('calibrate','C-resource-calibrate'),('formal','baseline')]:
        p=json.loads((root/('progress-'+phase+'.json')).read_text())
        for item in p['outcomes']:
            state=json.loads((root/directory/item['case_hash']/'state.json').read_text());case=state['case'];last=state['attempts'][-1]
            check_peer_result(case,last)
            checked=audit(case,last);audits.append(checked)
            if checked['issues']:raise ValueError(str(checked['issues']))
            if phase!='formal':continue
            path=Path(last['runPath']);read=lambda n:json.loads((path/(n+'.json')).read_text())
            m,s,w,o=map(read,('manifest','summary','windows','observer'));metrics=dict.fromkeys(CORE+WEIGHTS);evidence={}
            request_metrics(m,s,w,metrics,evidence)
            observer_metrics(o,m,None,metrics,evidence)  # No lane1 single-NIC calibration: Jain/physical utilization unavailable.
            if evidence.get('observer_complete') is not True:raise ValueError('observer incomplete')
            rows.append(dict(repeat=case['repeat'],profile='saturated' if case['parameters']['rate']==0 else '20pct',
                status=last['status'],run_path=str(path),metrics=metrics,counts=m['total'],
                receiver_contract=case['receiver_contract'],context=case['context']))
    buckets=defaultdict(list)
    for row in rows:buckets[row['profile']].append(row)
    groups=[]
    for profile,items in sorted(buckets.items()):
        if len(items)!=5 or {x['repeat'] for x in items}!={1,2,3,4,5}:raise ValueError('resource repeat coverage differs')
        keys=('goodput_gbps','p99_ms','throughput_250ms_cv','p99_window_cv','allocation_250ms_share_sd_erdma_0',
              'consumed_changes_0.001_per_second','ewma_lower_hit_fraction','retry_events')
        groups.append(dict(profile=profile,runs=5,metrics={k:interval([x['metrics'].get(k) for x in items]) for k in keys}))
    report=dict(scope='C matched-lane resource comparison only; both endpoints Worker/QP/CQ resources change',
        input_reference='20% of original default-lane A capacity, plus fixed-Q saturation; not resource-capacity 20%',
        limitations=['default A comparison is across dates; restoration control reported separately',
                     'no independent single-NIC lane1 capacities; no physical-utilization/Jain claim'],
        formal_requests=sum(r['counts']['accepted'] for r in rows),
        formal_failure=sum(r['counts']['failure'] for r in rows),formal_pending=sum(r['counts']['pending'] for r in rows),
        audited_attempts=len(audits),audited_records=sum(x['records'] for x in audits),audits=audits,groups=groups,runs=rows)
    with (output/'summary.json').open('x') as stream:json.dump(report,stream,indent=2)
    print('C_RESOURCE_ANALYSIS_COMPLETE',len(rows),len(audits),report['audited_records'])


if __name__=='__main__':main()
