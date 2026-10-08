#!/usr/bin/env python3
"""Current-main results only; no historical algorithm comparison."""
import json
import statistics
from pathlib import Path
from build_main_selection import ROOT
from main_baseline_metrics import validate,load_trace
from oscillation_metrics import diagnose
from summarize_baseline import CORE,WEIGHTS,request_metrics
import run_suite_collection_v2 as suite


def main():
    if (ROOT/'analysis.json').exists():raise ValueError('existing analysis retained')
    progress=suite._read(ROOT/'progress-formal.json')
    if not progress['complete']:raise ValueError('formal collection incomplete')
    cases={suite.case_hash(c):c for c in suite._read(ROOT/'plan-formal.json')['cases']};rows=[]
    for item in progress['outcomes']:
        c=cases[item['case_hash']];root=Path(item['run_path']);audit=validate(c,root)
        m,s,w=[suite._read(root/(n+'.json')) for n in ('manifest','summary','windows')]
        metrics=dict.fromkeys(CORE+WEIGHTS);evidence={};request_metrics(m,s,w,metrics,evidence)
        row=dict(profile=c['load'],variant=c['variant'],repeat=c['repeat'],run_path=str(root),performance=metrics,evidence=evidence,audit=audit,counts=m['total'])
        if c['traced']:
            _,trace=load_trace(root);normal=[r for r in trace if r['mode']==1]
            times=[(r['ns']-m['measurement_start_ns'])/1e9 for r in normal]
            column=str(audit['rail0_column'])
            row['weight']=diagnose(times,[r['weight'+column] for r in normal])
            row['allocation']=diagnose(times,[r['assigned'+column]/r['total_bytes'] for r in normal])
        rows.append(row)
    groups=[]
    for profile in ('225rps','675rps','saturated'):
        stock={r['repeat']:r for r in rows if r['profile']==profile and r['variant']=='stock'}
        pairs=[dict(repeat=r['repeat'],p99_ratio=r['performance']['p99_ms']/stock[r['repeat']]['performance']['p99_ms'],
                    goodput_ratio=r['performance']['goodput_gbps']/stock[r['repeat']]['performance']['goodput_gbps'])
               for r in rows if r['profile']==profile and r['variant']=='trace']
        gate=statistics.median(p['p99_ratio'] for p in pairs)<=1.20 and statistics.median(p['goodput_ratio'] for p in pairs)>=.95
        groups.append(dict(profile=profile,observation_gate_pass=gate,pairs=pairs,
            stock_medians={k:statistics.median(r['performance'][k] for r in stock.values()) for k in ('p99_ms','goodput_gbps')}))
    result=dict(runs=rows,groups=groups,scope='Only freshly merged-main original scheduling. Stock library is the performance baseline; trace variants measure observation sensitivity, not an algorithm comparison.',
        caveats=['Failed observation gates limit the interpretation of trace-build measurements.',
                 'Weight metrics apply to the trace build; actual per-request assignment changes are not migration of posted data.',
                 'Saturation is closed-loop Q128, not a claim of peak hardware capacity.'])
    suite._save(ROOT/'analysis.json',result,exclusive=True);print('MAIN_ANALYSIS_COMPLETE',json.dumps(groups),flush=True)


if __name__=='__main__':main()
