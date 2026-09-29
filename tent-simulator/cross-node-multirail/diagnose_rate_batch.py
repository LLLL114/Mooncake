#!/usr/bin/env python3
"""Post-measurement diagnostics from existing records; never starts traffic."""
import json
from pathlib import Path
import statistics
from rate_batch_metrics import load_trace
from run_rate_batch import ROOT


def main():
    data = json.loads((ROOT/'formal-analysis.json').read_text())
    plan = json.loads((ROOT/'plan-formal.json').read_text())
    import run_suite_collection_v2 as suite
    cases = {suite.case_hash(c):c for c in plan['cases']}
    result = []
    for row in data['runs']:
        case = cases[row['case_hash']]; p = Path(row['run_path'])
        manifest = json.loads((p/'manifest.json').read_text())
        _, records = load_trace(p)
        lo = manifest['measurement_start_ns'] + 10000000000
        hi = manifest['measurement_end_ns']
        normal = [r for r in records if r['mode']==1 and lo<=r['ns']<hi]
        n = len(normal)
        extreme_flips = sum(
            b['sequence']==a['sequence']+1 and b['ns']-a['ns']<=1000000000 and
            ((a['assigned0']==1048576 and b['assigned0']==0) or
             (a['assigned0']==0 and b['assigned0']==1048576))
            for a,b in zip(normal,normal[1:]))
        item = dict(variant=row['variant'],profile=row['profile'],repeat=row['repeat'],
            samples=n,all_to_rail0=sum(r['assigned0']==1048576 for r in normal)/n,
            all_to_rail1=sum(r['assigned0']==0 for r in normal)/n,
            target_at_zero_or_one=sum(r['weight0'] in (0.,1.) for r in normal)/n,
            adjacent_full_rail_flips=extreme_flips,full_rail_flips_per_second=extreme_flips/50,
            absolute_inflight_gap_at_least_request=sum(abs(r['inflight0']-r['inflight1'])>=1048576 for r in normal)/n,
            capacity0_median_bps=statistics.median(r['bandwidth0'] for r in normal),
            capacity1_median_bps=statistics.median(r['bandwidth1'] for r in normal),
            capacity_sum_median_bps=statistics.median(r['bandwidth0']+r['bandwidth1'] for r in normal),
            offered_bps=case['parameters']['rate'],p99_ms=row['performance']['p99_ms'],
            allocation_call_mean_ns=row['allocation_call_mean_ns'],
            submission_count_cv=row['input']['submitted_requests_per_second']['cv'],
            near_periodic=row['diagnoses']['weight']['0.05']['near_periodic'])
        if case['parameters'].get('rate_batch_policy',-1)>=2:
            item['capacity_sum_vs_offered'] = item['capacity_sum_median_bps']/item['offered_bps']
            item['capacity0_vs_seed'] = item['capacity0_median_bps']/case['parameters']['rail0_bps']
            item['capacity1_vs_seed'] = item['capacity1_median_bps']/case['parameters']['rail1_bps']
        result.append(item)
    groups = []
    for policy in ('reference','original','legacy-remainder','fixed-remainder','aggregate-remainder','fixed-batch','aggregate-batch'):
        for profile in ('20pct','60pct'):
            rows = [r for r in result if r['variant']==policy and r['profile']==profile]
            group = dict(variant=policy,profile=profile)
            for key in ('all_to_rail0','all_to_rail1','target_at_zero_or_one','full_rail_flips_per_second',
                        'absolute_inflight_gap_at_least_request','capacity0_median_bps','capacity1_median_bps',
                        'capacity_sum_vs_offered','capacity0_vs_seed','capacity1_vs_seed','allocation_call_mean_ns','submission_count_cv'):
                values = [r[key] for r in rows if key in r]
                group[key] = statistics.median(values) if values else None
            group['all_p99_ms'] = [r['p99_ms'] for r in rows]
            group['all_full_rail_flips'] = [r['adjacent_full_rail_flips'] for r in rows]
            group['near_periodic_runs'] = sum(r['near_periodic'] for r in rows)
            groups.append(group)
    totals = {}
    for stage in ('calibration','pilot','overhead','formal'):
        progress = json.loads((ROOT/('progress-'+stage+'.json')).read_text())
        roots = [Path(x['run_path']) for x in progress['outcomes']]
        totals[stage] = dict(runs=len(roots),accepted=sum(json.loads((x/'manifest.json').read_text())['total']['accepted'] for x in roots))
    output = ROOT/'postmeasurement-diagnostics.json'
    if output.exists():
        raise ValueError('diagnostics already exist')
    output.write_text(json.dumps(dict(scope='existing traces only, primary interval10-60s',
        definition='full-rail flips are adjacent normal decisions without intervening probe; these are new-request allocations, not in-flight migration',
        totals=totals,groups=groups,runs=result),indent=2)+'\n')
    print('RB_DIAGNOSTIC_TOTALS',json.dumps(totals),flush=True)
    for group in groups:
        print('RB_DIAGNOSTIC',json.dumps(group),flush=True)


if __name__=='__main__':
    main()
