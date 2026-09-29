#!/usr/bin/env python3
"""Offline RB results with explicit factorial comparisons and learning validity."""
import argparse
from pathlib import Path
import statistics
import analyze_oscillation as analysis
from oscillation_metrics import describe, diagnose, read_json
from rate_batch_metrics import load_trace
from run_rate_batch import ROOT, POLICIES, validate
import run_suite_collection_v2 as suite


def ratio(a, b):
    return a / b if a is not None and b is not None and b > 0 else None


def pairs(rows, control, treatment):
    controls = {(r['profile'],r['repeat']): r for r in rows if r['variant'] == control}
    result = []
    for r in rows:
        if r['variant'] != treatment:
            continue
        base = controls[(r['profile'],r['repeat'])]
        out = dict(control=control, treatment=treatment, profile=r['profile'], repeat=r['repeat'])
        for name in ('goodput_gbps','p99_ms','throughput_250ms_cv','p99_window_cv'):
            out[name + '_ratio'] = ratio(r['performance'][name],base['performance'][name])
        out['allocation_mean_ratio'] = ratio(r['allocation_call_mean_ns'],base['allocation_call_mean_ns'])
        for family in ('weight','allocation_250ms'):
            if family not in r['diagnoses']:
                continue
            for metric in ('p95_p05','cycles_per_second'):
                out[family + '_' + metric + '_ratio'] = ratio(r['diagnoses'][family]['0.05'][metric],base['diagnoses'][family]['0.05'][metric])
        result.append(out)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['overhead','formal'], required=True)
    args = parser.parse_args()
    progress = read_json(ROOT / ('progress-' + args.stage + '.json'))
    if progress.get('complete') is not True:
        raise ValueError('measurement stage incomplete; do not analyze during traffic')
    target = ROOT / (args.stage + '-analysis.json')
    if target.exists():
        raise ValueError('existing analysis must not be overwritten')
    cases = {suite.case_hash(c):c for c in read_json(ROOT / ('plan-' + args.stage + '.json'))['cases']}
    analysis.validate, analysis.load_trace = validate, load_trace
    folder = ROOT / 'analysis' / args.stage; folder.mkdir(parents=True,exist_ok=False)
    rows = []
    for item in progress['outcomes']:
        case = cases[item['case_hash']]
        r, detail = analysis.analyze_case(case,dict(runPath=item['run_path'],status=item['status']))
        root = Path(item['run_path']); observer = read_json(root / 'observer.json')
        begin = 200 if args.stage == 'formal' else 0
        allocate = [x for t in observer['threads'] for x in t['rows'] if x['kind']=='allocate_call' and x['bin']>=begin]
        r['allocation_call_mean_ns'] = sum(x['values'][0]['sum'] for x in allocate) / sum(x['n'] for x in allocate)
        if args.stage == 'formal':
            m = read_json(root / 'manifest.json'); _,records = load_trace(root)
            records = [x for x in records if x['mode']==1]
            times = [(x['ns']-m['measurement_start_ns'])/1e9 for x in records]
            r['diagnoses']['inverse_score_diagnostic'] = {}
            inverse = []
            for x in records:
                v = [1/(x['score'+str(j)]+1e-12) for j in (0,1)]
                inverse.append(v[0]/sum(v))
            for threshold in analysis.THRESHOLDS:
                d = diagnose(times,inverse,threshold=threshold)
                r['diagnoses']['inverse_score_diagnostic'][str(threshold)] = analysis.compact(d)
            selected = [x for x,t in zip(records,times) if 10<=t<60]
            r['actual_weight_changed_fraction'] = sum(a['weight0']!=b['weight0'] for a,b in zip(selected,selected[1:])) / (len(selected)-1)
            r['scoring_bandwidth_bps'] = {str(j):describe([x['bandwidth'+str(j)] for x in selected]) for j in (0,1)}
            r['weight_meaning'] = 'continuous batch completion target' if case['parameters'].get('rate_batch_policy',-1)>=4 else 'normalized inverse score'
        rows.append(r)
        suite._save(folder / (item['case_hash'] + '.json'),dict(summary=r,turns=detail),exclusive=True)
    report = analysis.summarize(args.stage,rows)
    report['caveats'] = ['single-caller, equal 64KiB slices, no new fault or dynamic-load tests',
        'fixed effective rates include measured NUMA path cost; four new groups do not multiply the old penalty again',
        'inflight is a work proxy, L=0; shared receiver means independent-rate model may be approximate',
        'rate validity uses outstanding-work proxy, not complete application-limited identification',
        'constant offered input is not a saturated throughput noninferiority test']
    if args.stage == 'overhead':
        report['pairs'] = pairs(rows,'reference','original')
        report['stop_for_overhead'] = statistics.median(x['goodput_gbps_ratio'] for x in report['pairs']) < .95
    else:
        comparisons = [('reference',v) for v in POLICIES] + [('original',v) for v in POLICIES[1:]] + [
            ('fixed-remainder','aggregate-remainder'),('fixed-remainder','fixed-batch'),
            ('aggregate-remainder','aggregate-batch'),('fixed-batch','aggregate-batch')]
        report['pairs'] = [x for a,b in comparisons for x in pairs(rows,a,b)]
    suite._save(target,report,exclusive=True)
    if args.stage == 'formal':
        lines = ['# RB 速率估计与整批分配交叉消融','',
            f"正式{len(rows)}次，含预热{report['total_requests']}请求，failure={report['failure']}，pending={report['pending']}。",'',
            '以下为三次重复各自统计量的中位；持续往返判据与O/CAND一致。','',
            '| 策略 | 输入 | 权重跨度pp | 往返/s | 持续往返 | 250ms分配跨度pp | Gbps | P99ms |',
            '| --- | --- | ---: | ---: | --- | ---: | ---: | ---: |']
        for g in report['groups']:
            w,a=[g['series'][name]['0.05'] for name in ('weight','allocation_250ms')]
            lines.append(f"| {g['variant']} | {g['profile']} | {w['p95_p05_pp']['median']:.3f} | {w['cycles_per_second']['median']:.3f} | {w['sustained_runs']}/3 | {a['p95_p05_pp']['median']:.3f} | {g['goodput_gbps']['median']:.4f} | {g['p99_ms']['median']:.3f} |")
        lines += ['', '## 聚合估计器是否实际更新','',
            '| 策略 | 输入 | 重复 | 每轨测量窗口有效更新/观察次数 |',
            '| --- | --- | ---: | --- |']
        for r in rows:
            estimator = r['audit'].get('estimator',{})
            if estimator:
                values = ', '.join(f"dev{d}: {x['measurement_valid']}/{x['measurement_events']}" for d,x in estimator.items())
                lines.append(f"| {r['variant']} | {r['profile']} | {r['repeat']} | {values} |")
        lines += ['', '所有逐次结果、性能比值、四组交叉比较、计数与容量轨迹见formal-analysis.json及analysis/formal/。',
            '容量标定和去除重复NUMA惩罚是显式改变，不能把original到fixed的所有收益归因于新估计器。',
            '批量weight是连续目标，实际整数配额另验；有效学习为0不能称自适应成功。','']
        (ROOT/'RATE_BATCH_REPORT.md').write_text('\n'.join(lines))
    print('RB_ANALYSIS_COMPLETE',args.stage,len(rows),report.get('stop_for_overhead'),flush=True)


if __name__ == '__main__':
    main()
