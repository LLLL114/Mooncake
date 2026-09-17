#!/usr/bin/env python3
"""SSH-only structural checks; no RDMA traffic or synthetic performance claims."""
from analyze_E import COMPARE, paired_interval, summarize


def rows(variants):
    result = []
    for repeat in range(1, 6):
        for variant in variants:
            for profile in ('90pct', 'saturated'):
                result.append(dict(variant=variant, profile=profile, repeat=repeat,
                    metrics={k: 1.0 for k in (*COMPARE, 'ewma_lower_hit_fraction')},
                    audit=dict(updates=100, skipped=0), counts=dict(accepted=100)))
    return result


def main():
    assert paired_interval([1] * 5)['bootstrap95'] == [1, 1]
    assert paired_interval([1] * 4)['bootstrap95'] is None
    assert paired_interval([None] * 5)['median'] is None
    data = rows(['original', 'control'])
    for r in data:
        if r['variant'] == 'control':
            r['metrics']['goodput_gbps'] = .94
    assert summarize('overhead', data)['stop_for_overhead']
    data = rows(['control', 'k10', 'k100', 'a050', 'a090', 'a099'])
    baseline = summarize('steady', data)
    assert baseline['selection_is_diagnostic_only']
    assert len(baseline['selected']) == 1
    for r in data:
        if r['variant'] == 'k10':
            r['metrics']['throughput_250ms_cv'] = .7
            r['metrics']['p99_window_cv'] = .9
        if r['variant'] == 'k100':
            r['metrics']['throughput_250ms_cv'] = .5
            r['metrics']['p99_ms'] = 1.2
    selected = summarize('steady', data)
    assert selected['selected'] == ['k10']
    assert not selected['selection_is_diagnostic_only']
    try:
        summarize('steady', data[:-1])
    except ValueError:
        pass
    else:
        raise AssertionError('missing repetition accepted')
    print('E_ANALYSIS_STRUCTURAL_CHECKS_PASS: intervals, overhead, selection, missing data')


if __name__ == '__main__':
    main()
