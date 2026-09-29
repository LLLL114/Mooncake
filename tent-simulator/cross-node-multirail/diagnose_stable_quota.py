#!/usr/bin/env python3
"""Post-measurement SQ diagnostics: actual quota jumps, controller use, paired facts."""
from collections import Counter
import json
from pathlib import Path
import statistics
from oscillation_metrics import load_trace, read_json
from run_stable_quota import ROOT
from stable_quota_metrics import RECORD


def median(values):
    return statistics.median(values) if all(v is not None for v in values) else None


def main():
    formal, saturated = [read_json(ROOT/(s+'-analysis.json')) for s in ('formal','saturated')]
    diagnostics = []
    for row in formal['runs']:
        root = Path(row['run_path']); m = read_json(root/'manifest.json')
        start, end = m['measurement_start_ns']+10000000000, m['measurement_end_ns']
        _, records = load_trace(root)
        normal = [r for r in records if r['mode']==1 and start<=r['ns']<end]
        # Include only consecutive decisions; probe boundaries are excluded.
        adjacent = [(a,b) for a,b in zip(normal,normal[1:]) if b['sequence']==a['sequence']+1]
        jumps = Counter(abs(b['assigned0']-a['assigned0'])//65536 for a,b in adjacent)
        d = dict(variant=row['variant'],profile=row['profile'],repeat=row['repeat'],
            adjacent_normal_pairs=len(adjacent),slice_jump_counts=dict(jumps),max_slice_jump=max(jumps),
            changes_per_second=sum(n for k,n in jumps.items() if k)/50,
            quota_counts=dict(Counter(r['assigned0']//65536 for r in normal)))
        if row['variant'] != 'reference':
            control = [x for x in RECORD.iter_unpack((root/'stable-quota.bin').read_bytes()) if start<=x[0]<end]
            d['offset_samples'] = dict(Counter(x[-1] for x in control))
            d['offset_changes'] = sum(a[-1]!=b[-1] for a,b in zip(control,control[1:]))
        diagnostics.append(d)
    facts = []
    for stage,report in (('formal',formal),('saturated',saturated)):
        for group in report['groups']:
            rows = [r for r in report['runs'] if r['variant']==group['variant'] and r['profile']==group['profile']]
            out = dict(stage=stage,variant=group['variant'],profile=group['profile'])
            for name in ('goodput_gbps','p99_ms','throughput_250ms_cv','p99_window_cv'):
                out[name] = median([r['performance'][name] for r in rows])
            for name in ('allocation_call_mean_ns','actual_weight_changed_fraction','normal_quota_changed_fraction',
                         'normal_quota_changes_per_second','whole_measurement_rail0_byte_share'):
                out[name] = median([r[name] for r in rows])
            pp = [p for p in report['pairs'] if p['treatment']==group['variant'] and p['profile']==group['profile']]
            if pp:
                out['paired_medians'] = {k:median([p[k] for p in pp]) for k in pp[0] if k.endswith('_ratio')}
                out['p99_ratios'] = [p['p99_ms_ratio'] for p in pp]
                out['goodput_ratios'] = [p['goodput_gbps_ratio'] for p in pp]
            facts.append(out)
    counts = {}
    for stage in ('pilot','formal','saturated'):
        progress = read_json(ROOT/('progress-'+stage+'.json'))
        totals = [read_json(Path(x['run_path'])/'manifest.json')['total'] for x in progress['outcomes']]
        counts[stage] = dict(runs=len(totals),**{k:sum(t[k] for t in totals) for k in ('accepted','success','failure','pending')})
    failed = read_json(ROOT/'pilot-v1-receiver-schema/progress-pilot.json')
    counts['schema_failed_pilot'] = read_json(Path(failed['outcomes'][0]['run_path'])/'manifest.json')['total']
    output = dict(facts=facts,diagnostics=diagnostics,counts=counts)
    path = ROOT/'postmeasurement-diagnostics.json'
    if path.exists():
        raise ValueError('diagnostics already exist')
    path.write_text(json.dumps(output,indent=2)+'\n')
    print('SQ_DIAGNOSTICS_COMPLETE',flush=True)


if __name__ == '__main__':
    main()
