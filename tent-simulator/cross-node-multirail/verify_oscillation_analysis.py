#!/usr/bin/env python3
"""SSH-only: check group reproducibility and real pilot allocation grids."""
from copy import deepcopy
import json
from pathlib import Path

from analyze_oscillation import grids, summarize, THRESHOLDS
from run_oscillation import ROOT


def main():
    rows = []
    for repeat in (1, 2, 3):
        d = dict(sustained_back_and_forth=repeat != 3, near_periodic=False, p95_p05=.2,
                 cycles_per_second=4., periods={'cv': .7}, full_cycles=200,
                 blocks=[{'passes': True}] * 5)
        rows.append(dict(variant='native-free', profile='60pct', repeat=repeat,
            performance={'goodput_gbps': 1., 'p99_ms': 1.}, counts={'accepted': 10, 'failure': 0, 'pending': 0},
            input={'submitted_requests_per_second': {'cv': .01}},
            native_counter={'reversals_5pp_per_10000': 100.},
            diagnoses={name: {str(h): deepcopy(d) for h in THRESHOLDS}
                       for name in ('weight', 'allocation_250ms')}))
    result = summarize('formal', rows)
    assert result['groups'][0]['series']['weight']['0.05']['sustained_runs'] == 2
    assert not result['groups'][0]['series']['weight']['0.05']['reproducible']
    rows[2]['diagnoses']['weight']['0.05']['sustained_back_and_forth'] = True
    assert summarize('formal', rows)['groups'][0]['series']['weight']['0.05']['reproducible']
    try:
        summarize('formal', rows[:2])
    except ValueError:
        pass
    else:
        raise AssertionError('missing repeat accepted')
    progress = json.loads((ROOT / 'progress-pilot.json').read_text())
    assert progress['complete'] and len(progress['outcomes']) == 4
    count = 0
    for item in progress['outcomes']:
        if not item['audit']['fixed_equal']:
            continue
        path = Path(item['run_path'])
        observer = json.loads((path / 'observer.json').read_text())
        manifest = json.loads((path / 'manifest.json').read_text())
        grid = grids(observer, manifest)
        assert all(w['allocation'] is None or w['allocation'] == .5 for w in grid)
        assert any(w['allocation'] == .5 for w in grid)
        count += 1
    assert count == 2
    print('O_ANALYSIS_VALIDATION_PASS: 2/3 vs 3/3, missing-repeat rejection, two real fixed-equal pilot grids')


if __name__ == '__main__':
    main()
