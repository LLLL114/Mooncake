#!/usr/bin/env python3
"""Offline E analysis. Only run after the corresponding measurement stage exits."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random
import statistics

from run_E import ROOT, VARIANTS, validate
import run_suite_collection_v2 as suite
from summarize_baseline import CORE, WEIGHTS, observer_metrics, request_metrics

COMPARE = ('goodput_gbps', 'p99_ms', 'throughput_250ms_cv', 'p99_window_cv',
           'allocation_250ms_share_sd_erdma_0', 'consumed_changes_0.001_per_second',
           'consumed_changes_per_10000_comparisons', 'consumed_tv_per_second')


def distribution(values):
    values = [x for x in values if isinstance(x, (int, float)) and math.isfinite(x)]
    if not values:
        return dict(n=0, median=None, minimum=None, maximum=None)
    return dict(n=len(values), median=statistics.median(values), minimum=min(values), maximum=max(values))


def paired_interval(values):
    result = distribution(values)
    if len(values) != 5 or result['n'] != 5:
        return {**result, 'bootstrap95': None}
    rng = random.Random(20260917)
    draws = sorted(statistics.median(rng.choices(values, k=5)) for _ in range(10000))
    return {**result, 'bootstrap95': [draws[249], draws[9749]],
            'scope': 'paired median ratio; five pairs, percentile bootstrap; no multiplicity correction'}


def supplemental_dynamic(data, manifest, records):
    if data['profile'] == 'step':
        output = []
        for transition in data['transitions']:
            phase = next(p for p in data['phases'] if p['name'] == transition['to'])
            confirmed = transition['throughput_settling'].get('confirmed_at_s')
            row = dict(to=transition['to'], confirmed_at_s=confirmed)
            if confirmed is not None:
                windows = [w for w in data['series_250ms'] if transition['at_s'] + confirmed <= w['left']
                           and w['right'] <= phase['end_s']]
                flags = [abs(w['gbps'] / phase['offered_gbps'] - 1) > .1 for w in windows]
                row['after_confirmation_outside_fraction'] = sum(flags) / len(flags) if flags else None
            output.append(row)
        return dict(transitions=output)
    output = []
    for event in data['pulse_events']:
        start = manifest['measurement_start_ns'] + round(event['at_s'] * 1e9)
        selected = [r for r in records if start <= r['planned_ns'] < start + 100000000]
        if not selected:
            raise ValueError('empty pulse cohort')
        last = max(r['finished_ns'] for r in selected)
        output.append(dict(at_s=event['at_s'], requests=len(selected),
            all_finished_before_next_pulse=last < start + 2000000000,
            last_completion_after_pulse_end_s=(last - start - 100000000) / 1e9,
            backlog_recovery_censored=event['recovery_confirmed_s'] is None))
    return dict(pulses=output)


def rows_for(stage):
    progress = suite._read(ROOT / ('progress-' + stage + '.json'))
    if progress.get('complete') is not True:
        raise ValueError('measurement stage incomplete; do not analyze concurrently')
    rows = []
    for item in progress['outcomes']:
        state = suite._read(ROOT / ('E-' + stage) / item['case_hash'] / 'state.json')
        case, attempt = state['case'], state['attempts'][-1]
        if len(state['attempts']) != 1 or attempt['status'] != 'success':
            raise ValueError('failed/retried attempts require explicit reporting')
        audit = validate(case, attempt)
        path = Path(attempt['runPath'])
        m, s, w, o = (suite._read(path / (n + '.json')) for n in ('manifest', 'summary', 'windows', 'observer'))
        metrics, evidence = dict.fromkeys(CORE + WEIGHTS), {}
        request_metrics(m, s, w, metrics, evidence)
        observer_metrics(o, m, None, metrics, evidence)
        if evidence.get('observer_complete') is not True or evidence.get('analysis_loss'):
            raise ValueError('observer evidence incomplete')
        count = metrics.get('consumed_comparisons')
        metrics['consumed_changes_per_10000_comparisons'] = (
            metrics['consumed_changes_0.001_per_second'] * case['parameters']['seconds'] * 10000 / count) if count else None
        for name in ('queue_ns', 'submission_to_finish_ns'):
            # Keep missing fields explicit; exact exported latency keys are preserved below.
            values = s['latency_success'].get(name)
            if isinstance(values, dict):
                metrics[name + '_p99_ms'] = values['p99'] / 1e6 if values.get('p99') is not None else None
        row = dict(variant=case['variant'], profile=case['load'], repeat=case['repeat'],
            case_hash=item['case_hash'], run_path=str(path), metrics=metrics, audit=audit,
            counts=m['total'], receiver=m['receiver'], latency_success=s['latency_success'],
            data_verified=True, observer_complete=True)
        if stage == 'dynamic':
            from analyze_B import run as analyze_dynamic
            dynamic = analyze_dynamic(case, attempt, None)
            records = [json.loads(line) for line in (path / 'requests.jsonl').read_text().splitlines()]
            row['dynamic'] = dynamic
            row['supplement'] = supplemental_dynamic(dynamic, m, records)
        rows.append(row)
        print('E_ANALYZED', stage, len(rows), flush=True)
    return rows


def summarize(stage, rows):
    buckets = defaultdict(list)
    for row in rows:
        buckets[(row['variant'], row['profile'])].append(row)
    control_name = 'original' if stage == 'overhead' else 'control'
    controls = {(r['profile'], r['repeat']): r for r in rows if r['variant'] == control_name}
    groups = []
    for (variant, profile), items in sorted(buckets.items()):
        if len(items) != 5 or {r['repeat'] for r in items} != {1, 2, 3, 4, 5}:
            raise ValueError('missing matched repetitions')
        pairs = {}
        if variant != control_name:
            for key in COMPARE:
                values = []
                for r in items:
                    value, base = r['metrics'].get(key), controls[(profile, r['repeat'])]['metrics'].get(key)
                    if isinstance(value, (float, int)) and isinstance(base, (float, int)) and base > 0:
                        values.append(value / base)
                pairs[key] = paired_interval(values)
        group = dict(variant=variant, profile=profile,
            metrics={k: distribution([r['metrics'].get(k) for r in items]) for k in (*COMPARE, 'ewma_lower_hit_fraction')},
            paired_ratios=pairs,
            updates=distribution([r['audit']['updates'] for r in items]),
            skipped=distribution([r['audit']['skipped'] for r in items]))
        groups.append(group)
    result = dict(stage=stage, scope='real cross-node TENT E experiment; no production algorithm replacement',
        definitions=dict(ratios='variant/control paired by profile and repeat; below1 means lower',
            p99='planned arrival to completion; 1s window >=500 successes, >=20 valid windows',
            traffic_migrations='not measured: retry and weight changes are not unique request migrations'),
        total_requests=sum(r['counts']['accepted'] for r in rows), failure=0, pending=0,
        groups=groups, runs=rows)
    if stage == 'overhead':
        g = next(g for g in groups if g['variant'] == 'control')
        ratio = g['paired_ratios']['goodput_gbps']
        result['stop_for_overhead'] = ratio['median'] < .95
        result['throughput_noninferiority_2pct_supported'] = ratio['bootstrap95'][0] >= .98
    if stage == 'steady':
        candidates = []
        for variant in VARIANTS:
            if variant in ('original', 'control'):
                continue
            selected = [g for g in groups if g['variant'] == variant]
            pairs = [g['paired_ratios'] for g in selected]
            throughput_ok = all(p['goodput_gbps']['median'] >= .98 for p in pairs)
            p99_ok = all(p['p99_ms']['median'] <= 1.05 for p in pairs)
            cvs = [p[k]['median'] for p in pairs for k in ('throughput_250ms_cv', 'p99_window_cv')
                   if p[k]['median'] is not None]
            eligible = throughput_ok and p99_ok and bool(cvs) and min(cvs) <= .8 and max(cvs) <= 1.1
            score = statistics.mean(cvs) if cvs else float('inf')
            candidates.append(dict(variant=variant, eligible=eligible, score=score,
                throughput_ok=throughput_ok, p99_ok=p99_ok,
                throughput_noninferiority_2pct_supported=all(p['goodput_gbps']['bootstrap95'][0] >= .98 for p in pairs)))
        candidates.sort(key=lambda c: (not c['eligible'], c['score'], c['variant']))
        result['candidates'] = candidates
        winners = [c['variant'] for c in candidates if c['eligible']][:2]
        result['selected'] = winners or [candidates[0]['variant']]
        result['selection_is_diagnostic_only'] = not bool(winners)
    return result


def write_markdown():
    available = {stage: suite._read(ROOT / (stage + '-analysis.json')) for stage in ('overhead', 'steady', 'dynamic')
                 if (ROOT / (stage + '-analysis.json')).exists()}
    lines = ['# E 实验报告：带宽学习频率与 EWMA 平滑', '',
        '本报告来自真实跨机 TENT 流量；没有加入新生产选路算法。', '',
        '**阶段状态：** ' + '；'.join(stage + ('完成' if stage in available else '尚未完成') for stage in ('overhead', 'steady', 'dynamic')), '',
        '固定双 Rail、1 MiB、Q128、单 caller、双方默认6 lanes、NUMA0。原始V2、K1实验控制分开；'
        'K是每个完成线程/NIC的有效反馈计数，alpha是旧值系数。', '',
        '60秒稳态，1秒P99窗口；每窗至少500成功请求且至少20个有效窗口。'
        '同一配置5次，表中均为运行间中位数。所有数据/构建位于仓库外。', '']
    ref = suite._read(ROOT / 'reference.json')
    lines += [f"本轮3次短测参考吞吐中位数：**{ref['d0_bytes_per_second'] * 8e-9:.4f} Gbps**。这不是硬件峰值。", '']
    for stage, report in available.items():
        lines += [f'## {stage}', '', f"{len(report['runs'])}次运行，含预热{report['total_requests']:,}个请求全部成功；failure/pending=0，最终数据校验和observer完整性通过。", '']
        if stage == 'dynamic':
            lines += ['动态全程跨阶段CV不能当作稳态震荡指标；下表按实际事件汇总。', '',
                '| 参数 | 场景 | 诊断结果 |', '| --- | --- | --- |']
            for variant in sorted({r['variant'] for r in report['runs']}):
                items = [r for r in report['runs'] if r['variant'] == variant]
                transitions = [t for r in items if r['profile'] == 'step' for t in r['supplement']['transitions']]
                confirmations = distribution([t['confirmed_at_s'] for t in transitions])
                outside = distribution([t.get('after_confirmation_outside_fraction') for t in transitions])
                pulses = [p for r in items if r['profile'] == 'pulse' for p in r['supplement']['pulses']]
                lines.append(f"| {variant} | 阶跃 | {confirmations['n']}/{len(transitions)}个转换确认恢复；确认时间中位数{confirmations['median']}秒；确认后再次出带比例中位数{outside['median']} |")
                lines.append(f"| {variant} | 脉冲 | {sum(p['all_finished_before_next_pulse'] for p in pulses)}/{len(pulses)}个脉冲群在下一脉冲前完成；积压恢复带删失{sum(p['backlog_recovery_censored'] for p in pulses)}个 |")
            lines += ['', '每段吞吐、排队/P99、转换方向和脉冲细节保存在dynamic-analysis.json，不能用恢复确认时间代替持续稳定。', '']
            continue
        lines += ['| 参数 | 负载 | Gbps | P99 ms | 吞吐CV | 窗口P99 CV | Rail0份额SD(pp) |',
                  '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
        for group in report['groups']:
            m = group['metrics']
            def f(k, factor=1):
                value = m[k]['median']
                return f'{value * factor:.4f}' if value is not None else 'N/A'
            lines.append(f"| {group['variant']} | {group['profile']} | {f('goodput_gbps')} | {f('p99_ms')} | {f('throughput_250ms_cv')} | {f('p99_window_cv')} | {f('allocation_250ms_share_sd_erdma_0', 100)} |")
        lines += ['', '配对结果（同负载/重复号；比值<1代表下降；区间为5对运行的百分位bootstrap95%，未做多重比较校正）：', '',
            '| 参数/负载 | 吞吐比值 [95%区间] | P99比值 | 吞吐CV比值 | P99 CV比值 |', '| --- | --- | --- | --- | --- |']
        for g in report['groups']:
            p = g['paired_ratios']
            if not p:
                continue
            r = p['goodput_gbps']
            lines.append(f"| {g['variant']}/{g['profile']} | {r['median']:.4f} {r['bootstrap95']} | {p['p99_ms']['median']} | {p['throughput_250ms_cv']['median']} | {p['p99_window_cv']['median']} |")
        lines += ['']
        if stage == 'overhead':
            lines += [f"触发>5%吞吐开销停止门槛：{report['stop_for_overhead']}；2%吞吐非劣区间支持：{report['throughput_noninferiority_2pct_supported']}。不触发停止不代表已证实开销为零。", '']
        else:
            lines += ['权重与学习指标（均为运行中位数；变化阈值TV>0.001）：', '',
                '| 参数/负载 | 变化次数/秒 | 每万次比较的变化数 | 权重TV/秒 | 更新数/秒 | 下限命中率 |',
                '| --- | ---: | ---: | ---: | ---: | ---: |']
            for g in report['groups']:
                m = g['metrics']
                lines.append(f"| {g['variant']}/{g['profile']} | {m['consumed_changes_0.001_per_second']['median']} | {m['consumed_changes_per_10000_comparisons']['median']} | {m['consumed_tv_per_second']['median']} | {g['updates']['median'] / 60:.2f} | {m['ewma_lower_hit_fraction']['median']} |")
            lines += ['']
            lines += [f"动态复核参数：{', '.join(report['selected'])}。仅诊断候选（没有参数通过全部筛选门槛）：{report['selection_is_diagnostic_only']}。", '']
    lines += ['## 边界', '',
        '- 尚不能准确统计同一请求的跨Rail迁移次数；retry、权重变化和相邻请求分配变化分别报告。',
        '- 单接收NIC是共享瓶颈；本轮未单独标定每条Rail容量，不把平均分配直接叫作物理利用率提升。',
        '- 每K次更新带来的更新次数降低是机械结果；不能单凭这一项宣布优化成功。',
        '- 五次重复的区间可能很宽；候选筛选具有选择偏差，动态复核使用独立流量数据。',
        '- 本轮没有故障注入，不外推所有故障场景无挂起。原始失败记录如存在，不可用重试成功覆盖。',
        '- 首次K100短测传输后因不支持2秒汇总窗口失败，且没有固定NUMA亲和性；保留为兼容性诊断，不计入正式样本。正式运行前协议v2改用1秒窗口。',
        '- 源码、库指纹、逐请求记录、校验结果、计划及配对明细均在E目录；没有重写A/B/C结论。', '']
    (ROOT / 'E_REPORT.md').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['overhead', 'steady', 'dynamic'], required=True)
    args = parser.parse_args()
    target = ROOT / (args.stage + '-analysis.json')
    if target.exists():
        raise ValueError('analysis already exists; inspect rather than silently overwrite')
    result = summarize(args.stage, rows_for(args.stage))
    suite._save(target, result, exclusive=True)
    write_markdown()
    print('E_ANALYSIS_COMPLETE', args.stage, flush=True)


if __name__ == '__main__':
    main()
