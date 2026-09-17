#!/usr/bin/env python3
"""Offline O analysis: directional-change evidence, not a performance optimizer."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import statistics

from oscillation_metrics import describe, diagnose, load_trace, read_json
from run_oscillation import ROOT, validate
import run_suite_collection_v2 as suite
from summarize_baseline import CORE, WEIGHTS, request_metrics

THRESHOLDS = (.01, .05, .10)


def compact(d):
    return {k: v for k, v in d.items() if k not in ('extrema', 'cycles')}


def grids(observer, manifest):
    start, end = manifest['measurement_start_ns'], manifest['measurement_end_ns']
    count = (end - start) // 250000000
    names = manifest['environment']['nic_id_to_name']
    values = {kind: [[0, 0] for _ in range(count)] for kind in ('allocation', 'post', 'cq_success')}
    for thread in observer['threads']:
        for row in thread['rows']:
            if row['kind'] not in values:
                continue
            index = row['bin'] // 5
            name = names.get(str(row['dev']))
            if not 0 <= index < count or name not in ('erdma_0', 'erdma_1'):
                raise ValueError('invalid complete-bin IO record')
            values[row['kind']][index][0 if name == 'erdma_0' else 1] += row['bytes']
    return [dict(time=(i + .5) * .25, **{kind: (pair[0] / sum(pair) if sum(pair) else None)
                                        for kind, series in values.items() for pair in [series[i]]})
            for i in range(count)]


def native_counts(observer, begin_bin, mapping):
    comparisons = 0; changes = [0] * 3; reversals = [0] * 3
    bin_ranges = []
    for thread in observer['threads']:
        contexts = {c['id']: c for c in thread['contexts']}
        for row in thread['rows']:
            c = contexts[row['context']]
            if (row['bin'] >= begin_bin and row['kind'] == 'weight' and c['mode'] == 1
                    and c['site'] == 1 and mapping.get(str(row['dev'])) == 'erdma_0'):
                bin_ranges.append((row['values'][0]['max'] - row['values'][0]['min']) * 100)
            if row['bin'] < begin_bin or row['kind'] != 'weight_tv' or c['mode'] != 1 or c['site'] != 1:
                continue
            comparisons += row['n']
            for j in range(3):
                changes[j] += row['flags'][j]; reversals[j] += row['flags'][j + 3]
    return dict(comparisons=comparisons, changes=changes, reversals=reversals,
                reversals_5pp_per_10000=10000 * reversals[2] / comparisons if comparisons else None,
                weight_range_within_50ms_pp=describe(bin_ranges),
                definition='old native counter: reversal of successive individual >delta steps, not hysteretic full cycles')


def input_stats(root, manifest, seconds):
    origin = manifest['measurement_start_ns']
    records = [json.loads(line) for line in (root / 'requests.jsonl').read_text().splitlines()]
    records = [r for r in records if r['phase'] == 'measurement']
    begin = 10 if seconds == 60 else 0
    planned = [0] * (seconds - begin); submitted = [0] * (seconds - begin)
    times, delays = [], []
    for r in records:
        for field, target in (('planned_ns', planned), ('submitted_ns', submitted)):
            t = (r[field] - origin) / 1e9
            if begin <= t < seconds:
                target[int(t) - begin] += 1
        t = (r['submitted_ns'] - origin) / 1e9
        if begin <= t < seconds:
            times.append(r['submitted_ns']); delays.append((r['submitted_ns'] - r['planned_ns']) / 1e6)
    times.sort()
    return dict(interval_seconds=[begin, seconds], planned_requests_per_second=describe(planned),
                submitted_requests_per_second=describe(submitted),
                submission_gap_ms=describe([(b - a) / 1e6 for a, b in zip(times, times[1:])]),
                pre_submission_wait_ms=describe(delays),
                warning='planned constant input does not imply perfectly uniform actual admission')


def analyze_case(case, attempt):
    audit = validate(case, attempt)
    root = Path(attempt['runPath'])
    manifest, summary, windows, observer = [read_json(root / (n + '.json')) for n in ('manifest', 'summary', 'windows', 'observer')]
    seconds = int(case['parameters']['seconds'])
    metrics, evidence = dict.fromkeys(CORE + WEIGHTS), {}
    request_metrics(manifest, summary, windows, metrics, evidence)
    result = dict(case_hash=suite.case_hash(case), run_path=str(root), variant=case['variant'],
                  profile=case['load'], repeat=case['repeat'], counts=manifest['total'], audit=audit,
                  performance={k: metrics.get(k) for k in CORE}, input=input_stats(root, manifest, seconds),
                  native_counter=native_counts(observer, 200 if seconds == 60 else 0,
                                               manifest['environment']['nic_id_to_name']))
    grid = grids(observer, manifest)
    result['grid_250ms'] = grid
    if not case['has_trace']:
        return result, None
    meta, rows = load_trace(root)
    origin = manifest['measurement_start_ns']
    mapping = manifest['environment']['nic_id_to_name']
    index = next(j for j in (0, 1) if mapping[str(rows[0]['dev' + str(j)])] == 'erdma_0')
    normal = [r for r in rows if r['mode'] == 1]
    amplified = []; adjacent = 0
    left = origin + (10000000000 if seconds == 60 else 0)
    for a, b in zip(normal, normal[1:]):
        if a['ns'] < left or b['sequence'] != a['sequence'] + 1 or b['ns'] - a['ns'] > 1000000000:
            continue
        adjacent += 1
        dw = abs(b['weight' + str(index)] - a['weight' + str(index)])
        da = abs(b['assigned' + str(index)] / b['total_bytes'] - a['assigned' + str(index)] / a['total_bytes'])
        if dw <= .01 and da >= .10:
            amplified.append(dict(before_s=(a['ns'] - origin) / 1e9, after_s=(b['ns'] - origin) / 1e9,
                before_weight=a['weight' + str(index)], after_weight=b['weight' + str(index)],
                before_slices=a['assigned' + str(index)] // 65536, after_slices=b['assigned' + str(index)] // 65536))
    result['rounding_amplification'] = dict(adjacent_normal_comparisons=adjacent, events=len(amplified),
        fraction=len(amplified) / adjacent if adjacent else None,
        definition='adjacent non-probe decisions: weight delta <=1pp, allocated-share delta >=10pp',
        first_examples=amplified[:5])
    series = {}
    for name, records in (('weight', normal), ('weight_with_probe_diagnostic', rows),
                          ('allocation_normal', normal), ('allocation_all', rows)):
        series[name] = ([(r['ns'] - origin) / 1e9 for r in records],
            [r['weight' + str(index)] if name.startswith('weight') else r['assigned' + str(index)] / r['total_bytes']
             for r in records], 1.)
    for kind in ('allocation', 'post', 'cq_success'):
        eligible = [w for w in grid if w[kind] is not None]
        series[kind + '_250ms'] = ([w['time'] for w in eligible], [w[kind] for w in eligible], .251)
    result['weight_meaning'] = 'shadow' if meta['fixed_equal'] else 'actually consumed outside probes'
    result['decisions'] = len(rows)
    result['diagnoses'] = {}
    details = {}
    if seconds == 60:
        for name, (times, values, gap) in series.items():
            result['diagnoses'][name] = {}
            for threshold in THRESHOLDS:
                d = diagnose(times, values, threshold=threshold, max_gap=gap)
                result['diagnoses'][name][str(threshold)] = compact(d)
                if threshold == .05:
                    details[name] = {k: d[k] for k in ('extrema', 'cycles')}
    probe = [i for i, r in enumerate(rows) if r['mode'] == 2]
    phase = defaultdict(list)
    for i, r in enumerate(rows):
        t = (r['ns'] - origin) / 1e9
        if (10 if seconds == 60 else 0) <= t < seconds:
            phase[(i - probe[0]) % 100].append(r['weight' + str(index)])
    result['probe_phase_weight_mean'] = {str(k): statistics.mean(v) for k, v in sorted(phase.items())}
    result['trace_metadata'] = meta
    return result, details


def summarize(stage, rows):
    groups = defaultdict(list)
    for r in rows:
        groups[(r['variant'], r['profile'])].append(r)
    output = []
    for (variant, profile), items in sorted(groups.items()):
        if len(items) != 3 or {r['repeat'] for r in items} != {1, 2, 3}:
            raise ValueError('missing frozen repetitions')
        group = dict(variant=variant, profile=profile, runs=3,
            goodput_gbps=describe([r['performance']['goodput_gbps'] for r in items]),
            p99_ms=describe([r['performance']['p99_ms'] for r in items]),
            native_reversals_5pp_per_10000=describe([r['native_counter']['reversals_5pp_per_10000'] for r in items]),
            submitted_per_second_cv=describe([r['input']['submitted_requests_per_second']['cv'] for r in items]))
        if stage == 'formal':
            group['series'] = {}
            for name in items[0]['diagnoses']:
                family = {}
                for threshold in THRESHOLDS:
                    d = [r['diagnoses'][name][str(threshold)] for r in items]
                    family[str(threshold)] = dict(
                        sustained_runs=sum(x['sustained_back_and_forth'] for x in d),
                        reproducible=all(x['sustained_back_and_forth'] for x in d),
                        near_periodic_runs=sum(x['near_periodic'] for x in d),
                        p95_p05_pp=describe([x['p95_p05'] * 100 for x in d if x['p95_p05'] is not None]),
                        cycles_per_second=describe([x['cycles_per_second'] for x in d]),
                        period_cv=describe([x['periods']['cv'] for x in d]),
                        full_cycles=[x['full_cycles'] for x in d],
                        passing_10s_blocks=[sum(b['passes'] for b in x['blocks']) for x in d])
                group['series'][name] = family
        output.append(group)
    report = dict(stage=stage, groups=output, runs=rows,
                  total_requests=sum(r['counts']['accepted'] for r in rows),
                  failure=sum(r['counts']['failure'] for r in rows), pending=sum(r['counts']['pending'] for r in rows),
                  caveats=['sustained back-and-forth and near-periodicity are distinct',
                           'computed fixed-equal weights are shadow diagnostics',
                           'symmetric priors do not make the physical paths identical',
                           'no claim of harmful self-excitation or throughput causality'])
    if stage == 'overhead':
        original = {r['repeat']: r for r in rows if r['variant'] == 'original'}
        pairs = []
        for r in rows:
            if r['variant'] != 'native-free':
                continue
            control = original[r['repeat']]
            pairs.append(dict(repeat=r['repeat'],
                goodput_ratio=r['performance']['goodput_gbps'] / control['performance']['goodput_gbps'],
                p99_ratio=r['performance']['p99_ms'] / control['performance']['p99_ms'],
                original_native_reversals=control['native_counter']['reversals_5pp_per_10000'],
                trace_native_reversals=r['native_counter']['reversals_5pp_per_10000'],
                original_50ms_weight_range_pp=control['native_counter']['weight_range_within_50ms_pp']['median'],
                trace_50ms_weight_range_pp=r['native_counter']['weight_range_within_50ms_pp']['median']))
        report['pairs'] = pairs
    return report


def write_report(report):
    lines = ['# O实验：均匀输入与固定均分下的权重往返', '',
             '这是重新建立震荡复现证据的实验，不是优化参数筛选。', '',
             f"正式矩阵{len(report['runs'])}次运行，含预热{report['total_requests']:,}个请求；failure={report['failure']}、pending={report['pending']}。", '',
             '主判据：第10–60秒，5pp滞回转折、P95−P5≥10pp、至少10完整往返，5个10秒块中至少4块各≥3往返；每条件3/3才称可重复。', '',
             '| 条件 | 权重意义 | 权重范围P95−P5(pp)中位 | 往返/s中位 | 持续往返重复数 | 近周期重复数 | 250ms分配持续往返重复数 |',
             '| --- | --- | ---: | ---: | --- | --- | --- |']
    for g in report['groups']:
        w = g['series']['weight']['0.05']; a = g['series']['allocation_250ms']['0.05']
        lines.append(f"| {g['variant']} / {g['profile']} | {'旁路' if g['variant'].endswith('equal') else '实际使用（排除probe）'} | {w['p95_p05_pp']['median']:.3f} | {w['cycles_per_second']['median']:.3f} | {w['sustained_runs']}/3 | {w['near_periodic_runs']}/3 | {a['sustained_runs']}/3 |")
    lines += ['', '注意：周期只是描述，不代表自激机理；两条权重的互补/负相关本身不是震荡证据。', '',
              '固定均分逐请求必须8/8，并有字节/原权重公式/整数分片规则/请求时间关联与50ms原观测交叉核验。',
              '原probe每100次仍运行；其计算权重仅作诊断，剔除和保留的结果分开保存。',
              '实际提交量CV和提交间隔保存在输入审计中，不把计划匀速冒充实际注入完全均匀。', '',
              '完整重复、1/5/10pp敏感性分析、转折点及周期位于formal-oscillation-analysis.json和analysis/formal/。',
              '图采用预先指定的重复2，全程与10–12秒放大，不挑选最极端的一次。', '']
    (ROOT / 'OSCILLATION_REPORT.md').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['overhead', 'formal'], required=True)
    args = parser.parse_args()
    progress = read_json(ROOT / ('progress-' + args.stage + '.json'))
    if progress.get('complete') is not True:
        raise ValueError('wait for complete measurements; do not analyze alongside traffic')
    target = ROOT / (args.stage + '-oscillation-analysis.json')
    if target.exists():
        raise ValueError('analysis exists; do not silently overwrite')
    folder = ROOT / 'analysis' / args.stage; folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in progress['outcomes']:
        state = read_json(ROOT / ('O-' + args.stage) / item['case_hash'] / 'state.json')
        if len(state['attempts']) != 1 or state['attempts'][0]['status'] != 'success':
            raise ValueError('failed/retried sample requires explicit treatment')
        row, details = analyze_case(state['case'], state['attempts'][0])
        if details is not None:
            suite._save(folder / (item['case_hash'] + '.json'), dict(case_hash=item['case_hash'], turns=details))
        rows.append(row)
        print('O_ANALYZED', args.stage, len(rows), flush=True)
    result = summarize(args.stage, rows)
    result['analysis_sources'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in [Path(__file__), Path(__file__).with_name('oscillation_metrics.py')]}
    suite._save(target, result, exclusive=True)
    if args.stage == 'formal':
        write_report(result)
    print('O_ANALYSIS_COMPLETE', args.stage, flush=True)


if __name__ == '__main__':
    main()
