#!/usr/bin/env python3
"""Offline reference-only performance and original weight diagnostics."""
import json
from pathlib import Path
import statistics
from oscillation_metrics import audit_trace,diagnose
from run_reference_rail_controls import ROOT,validate
import run_suite_collection_v2 as suite
from summarize_baseline import CORE,WEIGHTS,request_metrics


def main():
    if (ROOT/'analysis.json').exists(): raise ValueError('existing analysis retained')
    progress=suite._read(ROOT/'progress.json')
    if not progress['complete']: raise ValueError('incomplete collection')
    cases={suite.case_hash(c):c for c in suite._read(ROOT/'plan.json')['cases']}
    rows=[]
    for item in progress['outcomes']:
        c=cases[item['case_hash']];root=Path(item['run_path'])
        validate(c,dict(runPath=str(root),status=item['status']))
        m,s,w=[suite._read(root/(n+'.json')) for n in ('manifest','summary','windows')]
        metrics=dict.fromkeys(CORE+WEIGHTS);evidence={};request_metrics(m,s,w,metrics,evidence)
        row=dict(mode=c['mode'],profile=c['load'],repeat=c['repeat'],case_hash=item['case_hash'],run_path=str(root),status=item['status'],
            performance=metrics,evidence=evidence,counts=m['total'])
        if c['mode']=='D0':
            _,trace=audit_trace(root,False,False)
            trace=[r for r in trace if r['mode']==1]
            times=[(r['ns']-m['measurement_start_ns'])/1e9 for r in trace]
            row['weight']=diagnose(times,[r['weight0'] for r in trace])
            row['allocation']=diagnose(times,[r['assigned0']/r['total_bytes'] for r in trace])
        counter=ROOT/'counters'/item['case_hash']
        before,after=[suite._read(counter/(n+'.json')) for n in ('before','after')]
        row['sender_counter_deltas']={k:after[k]-v for k,v in before.items() if isinstance(v,int) and isinstance(after.get(k),int)}
        rows.append(row)
    groups=[]
    for mode in ('D0','S0','S1'):
        for profile in ('225rps','675rps'):
            matches=[r for r in rows if r['mode']==mode and r['profile']==profile]
            def stats(k):
                values=[r['performance'][k] for r in matches if r['performance'][k] is not None]
                return dict(median=statistics.median(values),minimum=min(values),maximum=max(values)) if values else None
            groups.append(dict(mode=mode,profile=profile,runs=len(matches),
                performance={k:stats(k) for k in ('goodput_gbps','p99_ms','throughput_250ms_cv','p99_window_cv')}))
    result=dict(runs=rows,groups=groups,caveats=[
        'Original O observation library, not a measurement-free build. New provider timing disabled.',
        'Equal offered rate does not mean equal utilization. No fresh per-Rail saturation calibration in RC.',
        'Receiver has one shared eRDMA NIC. Single-Rail contrasts change topology/queue resources and do not isolate weight causality.',
        'Per-window P99 CV uses 3s windows, each >=500 successful requests, >=20 complete valid windows.',
        'Sender RDMA hardware counters are per-device aggregates, not request-specific; no receiver RDMA hardware counters added. Existing sender/receiver network counters remain in manifests.'])
    suite._save(ROOT/'analysis.json',result,exclusive=True)
    print('RC_ANALYSIS_COMPLETE',json.dumps(groups),flush=True)


if __name__=='__main__':main()
