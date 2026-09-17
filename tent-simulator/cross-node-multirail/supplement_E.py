#!/usr/bin/env python3
"""Offline byte-share and update-weighted bandwidth diagnostics, after E ends."""
from collections import defaultdict
import json
from pathlib import Path
import statistics

from run_E import ROOT


def main():
    if (ROOT / 'pipeline.stage').read_text().strip() != 'complete':
        raise ValueError('wait until the measurement pipeline is complete')
    source = json.loads((ROOT / 'steady-analysis.json').read_text())
    rows = []
    for run in source['runs']:
        path = Path(run['run_path'])
        manifest = json.loads((path / 'manifest.json').read_text())
        observer = json.loads((path / 'observer.json').read_text())
        if observer['incomplete']:
            raise ValueError('incomplete observer')
        names = manifest['environment']['nic_id_to_name']
        totals = {kind: {name: 0 for name in names.values()} for kind in ('allocation', 'post', 'cq_success')}
        bw = {name: dict(updates=0, new_bandwidth_sum=0.) for name in names.values()}
        for thread in observer['threads']:
            for row in thread['rows']:
                name = names.get(str(row['dev']))
                if row['kind'] in totals:
                    if name is None:
                        raise ValueError('missing actual NIC mapping')
                    totals[row['kind']][name] += row['bytes']
                if row['kind'] == 'release_bw':
                    bw[name]['updates'] += row['n']
                    bw[name]['new_bandwidth_sum'] += row['values'][2]['sum']
        allocated = totals['allocation']
        count = sum(allocated.values())
        if count <= 0 or len(allocated) != 2:
            raise ValueError('not a measured dual-Rail allocation')
        fractions = {name: value / count for name, value in allocated.items()}
        duration_ns = manifest['measurement_end_ns'] - manifest['measurement_start_ns']
        rows.append(dict(variant=run['variant'], profile=run['profile'], repeat=run['repeat'],
            run_path=str(path), allocation_fraction=fractions,
            equal_rail_allocation_jain=1 / (2 * sum(f * f for f in fractions.values())),
            byte_totals=totals,
            cq_slice_payload_gbps={name: value * 8 / duration_ns for name, value in totals['cq_success'].items()},
            update_weighted_mean_bandwidth_gbps={name: v['new_bandwidth_sum'] / v['updates'] * 8e-9
                if v['updates'] else None for name, v in bw.items()}))
    groups = defaultdict(list)
    for r in rows:
        groups[(r['variant'], r['profile'])].append(r)
    summary = []
    for (variant, profile), items in sorted(groups.items()):
        if len(items) != 5:
            raise ValueError('missing repetitions')
        summary.append(dict(variant=variant, profile=profile,
            rail0_fraction_median=statistics.median(r['allocation_fraction']['erdma_0'] for r in items),
            equal_rail_jain_median=statistics.median(r['equal_rail_allocation_jain'] for r in items),
            learned_gbps_median={n: statistics.median(r['update_weighted_mean_bandwidth_gbps'][n] for r in items)
                for n in ('erdma_0', 'erdma_1')}))
    report = dict(definitions=dict(
        allocation_fraction='whole measured interval allocation-byte share, not a migration counter',
        equal_rail_jain='descriptive allocation balance assuming equal Rail targets; not capacity/NUMA-optimal utilization',
        cq_slice_payload_gbps='successful slice CQ bytes in the measured interval, not Ethernet wire speed or complete-request goodput',
        learned_bandwidth='new EWMA values weighted by update events; not a time-weighted state average; K changes the sampling population'),
        groups=summary, runs=rows)
    with (ROOT / 'traffic-and-bandwidth-supplement.json').open('x') as f:
        json.dump(report, f, indent=2)
    print('E_TRAFFIC_SUPPLEMENT_COMPLETE', len(rows))


if __name__ == '__main__':
    main()
