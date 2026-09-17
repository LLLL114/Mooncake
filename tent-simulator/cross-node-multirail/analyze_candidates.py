#!/usr/bin/env python3
"""Offline paired comparison; retain original O definitions and all repeats."""
from collections import defaultdict
import json
from pathlib import Path
import statistics

import analyze_oscillation as analysis
from candidate_metrics import load_trace
from oscillation_metrics import describe, diagnose, read_json
from run_candidates import ROOT, POLICIES, validate
import run_suite_collection_v2 as suite


def main():
    progress = read_json(ROOT / 'progress-formal.json')
    if progress.get('complete') is not True:
        raise ValueError('do not analyze while measurement is running')
    target = ROOT / 'candidate-analysis.json'
    if target.exists():
        raise ValueError('analysis already exists')
    plan = read_json(ROOT / 'plan-formal.json')
    cases = {suite.case_hash(c): c for c in plan['cases']}
    analysis.validate, analysis.load_trace = validate, load_trace
    rows = []
    folder = ROOT / 'analysis'; folder.mkdir(exist_ok=False)
    for item in progress['outcomes']:
        case = cases[item['case_hash']]
        attempt = dict(runPath=item['run_path'], status=item['status'])
        row, details = analysis.analyze_case(case, attempt)
        manifest = read_json(Path(item['run_path']) / 'manifest.json')
        _, records = load_trace(item['run_path'])
        normal = [r for r in records if r['mode'] == 1]
        times = [(r['ns'] - manifest['measurement_start_ns']) / 1e9 for r in normal]
        raw = []
        for r in normal:
            inv = [1 / (r['score' + str(j)] + 1e-12) for j in (0, 1)]
            raw.append(inv[0] / sum(inv))
        row['diagnoses']['raw_weight'] = {}
        for threshold in analysis.THRESHOLDS:
            d = diagnose(times, raw, threshold=threshold)
            row['diagnoses']['raw_weight'][str(threshold)] = analysis.compact(d)
            if threshold == .05:
                details['raw_weight'] = {k: d[k] for k in ('extrema', 'cycles')}
        # Native observer uses captured allocation timing; no new timer on the path.
        observer = read_json(Path(item['run_path']) / 'observer.json')
        alloc = [r for t in observer['threads'] for r in t['rows'] if r['kind'] == 'allocate_call' and r['bin'] >= 200]
        row['allocation_call_mean_ns'] = (sum(r['values'][0]['sum'] for r in alloc) / sum(r['n'] for r in alloc)) if alloc else None
        selected = [(r, w) for r, w, t in zip(normal, raw, times) if 10 <= t < 60]
        row['filter_absolute_deviation_pp'] = describe([100 * abs(r['weight0'] - w) for r, w in selected])
        row['applied_changed_fraction'] = (sum(a['weight0'] != b['weight0'] for (a, _), (b, _) in zip(selected, selected[1:])) / (len(selected) - 1)) if len(selected) > 1 else None
        rows.append(row)
        suite._save(folder / (item['case_hash'] + '.json'), dict(summary=row, turns=details), exclusive=True)
    report = analysis.summarize('formal', rows)
    by = {(r['variant'], r['profile'], r['repeat']): r for r in rows}
    pairs = []
    for r in rows:
        if r['variant'] == 'original':
            continue
        control = by[('original', r['profile'], r['repeat'])]
        pair = dict(variant=r['variant'], profile=r['profile'], repeat=r['repeat'])
        for name in ('goodput_gbps', 'p99_ms', 'throughput_250ms_cv', 'p99_window_cv'):
            a, b = r['performance'][name], control['performance'][name]
            pair[name + '_ratio'] = a / b if a is not None and b is not None and b > 0 else None
        a, b = r['allocation_call_mean_ns'], control['allocation_call_mean_ns']
        pair['allocation_call_mean_ratio'] = a / b if a is not None and b else None
        for name in ('weight', 'allocation_250ms'):
            a, b = r['diagnoses'][name]['0.05'], control['diagnoses'][name]['0.05']
            for metric in ('p95_p05', 'cycles_per_second'):
                pair[name + '_' + metric + '_ratio'] = a[metric] / b[metric] if b[metric] else None
        pairs.append(pair)
    report['pairs'] = pairs
    report['caveats'] = ['real independent closed-loop runs, not identical feedback replay',
        'constant input throughput is not saturated capacity', 'three repetitions do not prove population noninferiority',
        'raw weight and consumed filtered weight are separate', 'probe weights excluded from primary weight analysis',
        'fault, changing load, multithread and GPU variants not tested']
    suite._save(target, report, exclusive=True)
    lines = ['# CAND 同负载候选实测', '',
        f"正式{len(rows)}次，含预热{report['total_requests']}请求，failure={report['failure']}，pending={report['pending']}。", '',
        '各数值为三次重复统计量的中位；权重/分配主区间10–60秒，5pp滞回。', '',
        '| 策略 | 输入 | 原始权重跨度pp | 采用权重跨度pp | 往返/s | 持续往返 | 250ms分配跨度pp | Gbps | P99 ms |',
        '| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |']
    for g in report['groups']:
        w, raw, a = [g['series'][name]['0.05'] for name in ('weight', 'raw_weight', 'allocation_250ms')]
        lines.append(f"| {g['variant']} | {g['profile']} | {raw['p95_p05_pp']['median']:.3f} | {w['p95_p05_pp']['median']:.3f} | {w['cycles_per_second']['median']:.3f} | {w['sustained_runs']}/3 | {a['p95_p05_pp']['median']:.3f} | {g['goodput_gbps']['median']:.4f} | {g['p99_ms']['median']:.3f} |")
    lines += ['', '## 与同重复原算法配对比值', '',
        '| 策略 | 输入 | 吞吐比中位 | P99比中位 | 权重跨度比中位 | 权重往返率比中位 |',
        '| --- | --- | ---: | ---: | ---: | ---: |']
    for policy in POLICIES[1:]:
        for profile in ('20pct', '60pct'):
            group = [p for p in pairs if p['variant'] == policy and p['profile'] == profile]
            cols = [statistics.median(p[k] for p in group) for k in
                    ('goodput_gbps_ratio', 'p99_ms_ratio', 'weight_p95_p05_ratio', 'weight_cycles_per_second_ratio')]
            lines.append('| ' + ' | '.join([policy, profile] + [f'{v:.4f}' for v in cols]) + ' |')
    lines += ['', '完整逐次、配对比值、原始/采用权重诊断及分配调用均时见 candidate-analysis.json。',
        '本轮未做饱和吞吐、动态/故障恢复；不能单凭曲线更平就验收为生产优化。', '']
    (ROOT / 'CANDIDATE_REPORT.md').write_text('\n'.join(lines))
    print('CAND_ANALYSIS_COMPLETE', len(rows), report['failure'], report['pending'], flush=True)


if __name__ == '__main__':
    main()
