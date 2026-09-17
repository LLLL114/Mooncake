#!/usr/bin/env python3
"""Verify actual per-Rail bounds and NUMA penalties across all E measurements."""
import json
from pathlib import Path

from run_E import ROOT


def main():
    if (ROOT / 'pipeline.stage').read_text().strip() != 'complete':
        raise ValueError('wait for the complete pipeline')
    rows = []
    for stage in ('overhead', 'steady', 'dynamic'):
        report = json.loads((ROOT / (stage + '-analysis.json')).read_text())
        for run in report['runs']:
            p = Path(run['run_path'])
            m = json.loads((p / 'manifest.json').read_text())
            o = json.loads((p / 'observer.json').read_text())
            if o['incomplete']:
                raise ValueError('incomplete observer')
            names = m['environment']['nic_id_to_name']
            bounds = {name: set() for name in names.values()}
            penalties = {name: set() for name in names.values()}
            for t in o['threads']:
                for r in t['rows']:
                    name = names.get(str(r['dev']))
                    if r['kind'] == 'release_bounds':
                        for bound in ('min', 'max'):
                            bounds[name].add(tuple(round(v[bound] * 8e-9, 6) for v in r['values']))
                    if r['kind'] == 'candidate':
                        penalties[name].update((r['values'][2]['min'], r['values'][2]['max']))
            expected = {'erdma_0': {(100., 10., 1000.)}, 'erdma_1': {(10., 1., 100.)}}
            if bounds != expected or penalties != {'erdma_0': {1.}, 'erdma_1': {10.}}:
                raise ValueError(f'environment differs in {p}: {bounds}, {penalties}')
            rows.append(dict(stage=stage, variant=run['variant'], run_path=str(p), passed=True))
        print('ENVIRONMENT_AUDIT_STAGE', stage, len(rows), flush=True)
    result = dict(runs=len(rows), all_passed=True,
        observed_bounds_gbps={'erdma_0': dict(theoretical=100, lower=10, upper=1000),
                              'erdma_1': dict(theoretical=10, lower=1, upper=100)},
        observed_numa_penalty={'erdma_0': 1, 'erdma_1': 10},
        interpretation='algorithm inputs, not measured physical capacity; unknown-speed fallback400/lower40 is not active here',
        rows=rows)
    with (ROOT / 'environment-audit.json').open('x') as f:
        json.dump(result, f, indent=2)
    print('E_ENVIRONMENT_AUDIT_COMPLETE', len(rows), flush=True)


if __name__ == '__main__':
    main()
