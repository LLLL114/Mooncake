#!/usr/bin/env python3
"""TG joint acceptance: P99 improvement, weight stability and goodput together."""
import json
import math
from pathlib import Path
import statistics
import analyze_oscillation as analysis
from analyze_rate_batch import pairs, ratio
from oscillation_metrics import describe, load_trace, read_json, quantile
from run_tail_gain import ROOT, POLICIES, validate
import run_suite_collection_v2 as suite


def enrich(row, stage):
    root = Path(row['run_path']); m,s,o = [read_json(root/(n+'.json')) for n in ('manifest','summary','observer')]
    begin = m['measurement_start_ns']+(10000000000 if stage=='formal' else 0)
    _, records = load_trace(root)
    normal = [r for r in records if r['mode']==1 and r['ns']>=begin]
    adjacent = [(a,b) for a,b in zip(normal,normal[1:]) if b['sequence']==a['sequence']+1]
    duration = 50 if stage=='formal' else 30
    row['quota_changes_per_second'] = sum(a['assigned0']!=b['assigned0'] for a,b in adjacent)/duration
    row['max_slice_jump'] = max(abs(a['assigned0']-b['assigned0'])//65536 for a,b in adjacent)
    row['weight_changes_per_second'] = sum(a['weight0']!=b['weight0'] for a,b in zip(normal,normal[1:]))/duration
    calls = [r for t in o['threads'] for r in t['rows'] if r['kind']=='allocate_call' and r['bin']>=(200 if stage=='formal' else 0)]
    row['allocation_call_mean_ns'] = sum(r['values'][0]['sum'] for r in calls)/sum(r['n'] for r in calls)
    row['latency_parts_p99_ms'] = {k:v['p99']/1e6 for k,v in s['latency_success'].items() if isinstance(v,dict) and 'p99' in v and v['p99'] is not None}
    req = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
    req = [r for r in req if m['measurement_start_ns']<=r['planned_ns']<m['measurement_end_ns'] and r['status']=='success']
    tail = sorted(req,key=lambda r:r['finished_ns']-r['planned_ns'])[-max(1,math.ceil(len(req)*.01)):]
    row['slowest_one_percent'] = dict(count=len(tail),
        mean_injection_fraction=statistics.mean((r['submitted_ns']-r['planned_ns'])/(r['finished_ns']-r['planned_ns']) for r in tail))
    # Supplemental 5s cohort P99: descriptive only, fewer than the old 20-window gate.
    window_p99 = []
    for offset in range(0,int((m['measurement_end_ns']-m['measurement_start_ns'])/1e9),5):
        left=m['measurement_start_ns']+offset*1000000000
        values=[(r['finished_ns']-r['planned_ns'])/1e6 for r in req if left<=r['planned_ns']<left+5000000000]
        window_p99.append(quantile(values,.99) if len(values)>=500 else None)
    row['p99_5s_descriptive'] = describe(window_p99)
    row['p99_5s_windows'] = [dict(time=(i+.5)*5,p99_ms=v) for i,v in enumerate(window_p99)]
    if row['variant']=='gain':
        events = read_json(root/'TG-controller-windows.json')
        selected = [e for e in events if begin<=e['ns']<m['measurement_end_ns']]
        row['controller'] = dict(windows=len(selected),changes=sum(e['changed'] for e in selected),
            predicted_step_gain_ms=describe([e['actual_step_gain_seconds']*1000 for e in selected if e['changed']]))


def main():
    if (ROOT/'comparison.json').exists():
        raise ValueError('analysis already exists')
    analysis.validate,analysis.load_trace = validate,load_trace
    reports = {}
    for stage in ('formal','saturated'):
        p = read_json(ROOT/('progress-'+stage+'.json'))
        if not p.get('complete'):
            raise ValueError('measurement incomplete')
        cases={suite.case_hash(c):c for c in read_json(ROOT/('plan-'+stage+'.json'))['cases']}
        folder=ROOT/'analysis'/stage; folder.mkdir(parents=True,exist_ok=False)
        rows=[]
        for item in p['outcomes']:
            row,detail=analysis.analyze_case(cases[item['case_hash']],dict(runPath=item['run_path'],status=item['status']))
            enrich(row,stage); rows.append(row)
            suite._save(folder/(item['case_hash']+'.json'),dict(summary=row,turns=detail),exclusive=True)
        report=analysis.summarize(stage,rows)
        report['pairs']=[p for variant in POLICIES[1:] for p in pairs(rows,'reference',variant)]
        report['gain_vs_integer']=pairs(rows,'integer','gain')
        by_key={(r['variant'],r['profile'],r['repeat']):r for r in rows}
        for p in report['pairs']:
            a=by_key[(p['treatment'],p['profile'],p['repeat'])]; b=by_key[('reference',p['profile'],p['repeat'])]
            p['quota_changes_ratio']=ratio(a['quota_changes_per_second'],b['quota_changes_per_second'])
            p['p99_5s_cv_ratio']=ratio(a['p99_5s_descriptive']['cv'],b['p99_5s_descriptive']['cv'])
        report['caveats']=['prediction is a Q/C proxy, not an observed counterfactual or P99 model',
            '5-second P99 windows are descriptive; original insufficient-sample N/A is retained',
            'three repetitions and one topology only; no new dynamic/fault validation']
        reports[stage]=report; suite._save(ROOT/(stage+'-analysis.json'),report,exclusive=True)
    gates={}
    for policy in POLICIES[1:]:
        core,secondary={},{}
        gs=[p['goodput_gbps_ratio'] for p in reports['saturated']['pairs'] if p['treatment']==policy]
        core['saturated_goodput']=statistics.median(gs)>=.98 and min(gs)>=.95
        for profile in ('20pct','60pct'):
            pp=[p for p in reports['formal']['pairs'] if p['treatment']==policy and p['profile']==profile]
            vals=[p['p99_ms_ratio'] for p in pp]
            core[profile+'_p99_improvement']=statistics.median(vals)<=.90 and max(vals)<=1.05
            for key in ('weight_p95_p05_ratio','weight_cycles_per_second_ratio','allocation_250ms_p95_p05_ratio','quota_changes_ratio'):
                vals=[p[key] for p in pp]; limit=1 if key=='quota_changes_ratio' else .5
                core[profile+'_'+key]=statistics.median(vals)<=limit if all(v is not None for v in vals) else None
            rows=[r for r in reports['formal']['runs'] if r['variant']==policy and r['profile']==profile]
            core[profile+'_no_sustained_weight_oscillation']=not any(r['diagnoses']['weight']['0.05']['sustained_back_and_forth'] for r in rows)
            for key in ('throughput_250ms_cv_ratio','p99_window_cv_ratio'):
                vals=[p[key] for p in pp]
                secondary[profile+'_'+key]=statistics.median(vals)<=1 if all(v is not None for v in vals) else None
        for key in ('throughput_250ms_cv_ratio','p99_window_cv_ratio'):
            vals=[p[key] for p in reports['saturated']['pairs'] if p['treatment']==policy]
            secondary['saturated_'+key]=statistics.median(vals)<=1 if all(v is not None for v in vals) else None
        gates[policy]=dict(primary=core,secondary=secondary,
            joint_primary_pass=all(v is True for v in core.values()),
            full_status='failed' if False in [*core.values(),*secondary.values()] else
                        'inconclusive' if None in [*core.values(),*secondary.values()] else 'screening_pass_only')
    suite._save(ROOT/'comparison.json',dict(gates=gates),exclusive=True)
    print('TG_ANALYSIS_COMPLETE',json.dumps(gates),flush=True)


if __name__=='__main__':
    main()
