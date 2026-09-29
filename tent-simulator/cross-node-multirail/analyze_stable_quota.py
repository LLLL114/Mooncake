#!/usr/bin/env python3
"""Offline SQ comparison, with frozen gates and all paired repetitions retained."""
from pathlib import Path
import statistics
import analyze_oscillation as analysis
from analyze_rate_batch import pairs
from oscillation_metrics import load_trace, read_json
from run_stable_quota import ROOT, POLICIES, validate
import run_suite_collection_v2 as suite


def main():
    if (ROOT/'comparison.json').exists():
        raise ValueError('analysis exists')
    analysis.validate, analysis.load_trace = validate, load_trace
    reports = {}
    for stage in ('formal', 'saturated'):
        progress = read_json(ROOT/('progress-'+stage+'.json'))
        if not progress.get('complete'):
            raise ValueError('wait for all traffic to finish')
        cases = {suite.case_hash(c):c for c in read_json(ROOT/('plan-'+stage+'.json'))['cases']}
        rows = []
        folder = ROOT/'analysis'/stage; folder.mkdir(parents=True,exist_ok=False)
        for item in progress['outcomes']:
            c = cases[item['case_hash']]
            r, detail = analysis.analyze_case(c,dict(runPath=item['run_path'],status=item['status']))
            root = Path(item['run_path']); observer = read_json(root/'observer.json')
            left = 200 if stage == 'formal' else 0
            calls = [x for t in observer['threads'] for x in t['rows'] if x['kind']=='allocate_call' and x['bin']>=left]
            r['allocation_call_mean_ns'] = sum(x['values'][0]['sum'] for x in calls)/sum(x['n'] for x in calls)
            m = read_json(root/'manifest.json'); _, records = load_trace(root)
            normal = [x for x in records if x['mode']==1 and x['ns'] >= m['measurement_start_ns']+(10000000000 if stage=='formal' else 0)]
            comparisons = list(zip(normal,normal[1:]))
            r['actual_weight_changed_fraction'] = sum(a['weight0']!=b['weight0'] for a,b in comparisons)/len(comparisons)
            r['normal_quota_changed_fraction'] = sum(a['assigned0']!=b['assigned0'] for a,b in comparisons)/len(comparisons)
            r['normal_quota_changes_per_second'] = sum(a['assigned0']!=b['assigned0'] for a,b in comparisons)/(50 if stage=='formal' else 30)
            r['whole_measurement_rail0_byte_share'] = sum(x['assigned0'] for x in records)/sum(x['total_bytes'] for x in records)
            r['weight_meaning'] = 'normalized inverse score' if c['stable_policy']==0 else 'consumed quantized quota target'
            rows.append(r)
            suite._save(folder/(item['case_hash']+'.json'),dict(summary=r,turns=detail),exclusive=True)
        report = analysis.summarize(stage,rows)
        report['pairs'] = [p for policy in POLICIES[1:] for p in pairs(rows,'reference',policy)]
        report['caveats'] = ['one caller, two sender rails and one shared receiver rail',
            'reference includes O instrumentation; candidates add 48-byte per-normal-decision audit records',
            'fixed capacity includes earlier measured path cost, not physical independent link capacity',
            'table-driven quantization changes are not migrations of in-flight transfers',
            'no load-step, fault recovery, mixed-size or multi-caller evidence in this round',
            'three repetitions support screening only, not production noninferiority']
        suite._save(ROOT/(stage+'-analysis.json'),report,exclusive=True)
        reports[stage] = report
    gates = {}
    for policy in POLICIES[1:]:
        checks = {}
        saturation = [p for p in reports['saturated']['pairs'] if p['treatment']==policy]
        gs = [p['goodput_gbps_ratio'] for p in saturation]
        checks['saturated_goodput'] = statistics.median(gs)>=.98 and min(gs)>=.95
        for profile in ('20pct','60pct'):
            pp = [p for p in reports['formal']['pairs'] if p['treatment']==policy and p['profile']==profile]
            for metric in ('weight_p95_p05_ratio','allocation_250ms_p95_p05_ratio'):
                values = [p[metric] for p in pp]
                checks[profile+'_'+metric] = statistics.median(values)<=.5 if all(x is not None for x in values) else None
            values = [p['p99_ms_ratio'] for p in pp]
            checks[profile+'_p99'] = statistics.median(values)<=1.05 and max(values)<=1.10
            for metric in ('throughput_250ms_cv_ratio','p99_window_cv_ratio'):
                values = [p[metric] for p in pp]
                checks[profile+'_'+metric] = statistics.median(values)<=1 if all(x is not None for x in values) else None
        gates[policy] = dict(checks=checks,screening_pass=all(v is True for v in checks.values()),
            status='failed' if False in checks.values() else 'inconclusive' if None in checks.values() else 'screening_pass_only')
    suite._save(ROOT/'comparison.json',dict(gates=gates,repetitions=3),exclusive=True)
    lines = ['# SQ 稳定配额实验自动汇总','',
        '| 策略 | 输入 | 权重跨度pp | 往返/s | 250ms配额跨度pp | Gbps | P99ms |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for g in reports['formal']['groups']:
        w,a = [g['series'][k]['0.05'] for k in ('weight','allocation_250ms')]
        lines.append(f"| {g['variant']} | {g['profile']} | {w['p95_p05_pp']['median']:.3f} | {w['cycles_per_second']['median']:.3f} | {a['p95_p05_pp']['median']:.3f} | {g['goodput_gbps']['median']:.4f} | {g['p99_ms']['median']:.3f} |")
    lines += ['', '## 饱和对照','', '| 策略 | Gbps中位 | P99ms中位 |','| --- | ---: | ---: |']
    for g in reports['saturated']['groups']:
        lines.append(f"| {g['variant']} | {g['goodput_gbps']['median']:.4f} | {g['p99_ms']['median']:.3f} |")
    lines += ['', '## 冻结初筛','']
    for policy, result in gates.items():
        lines.append(f"- {policy}: {result['status']}；{result['checks']}")
    lines += ['', '逐次配对比、实际分片切换率、均时开销及全部失败计数见formal-analysis.json、saturated-analysis.json。',
        '这不是故障/动态收敛验收。固定配额的零权重波动是构造性质，是否可用仍取决于性能。','']
    (ROOT/'STABLE_QUOTA_REPORT.md').write_text('\n'.join(lines))
    print('SQ_ANALYSIS_COMPLETE',gates,flush=True)


if __name__ == '__main__':
    main()
