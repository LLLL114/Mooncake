#!/usr/bin/env python3
"""Render preselected repeat2 curves on the server, with isolated plot packages."""
import argparse
import json
import os
from pathlib import Path
import sys

BASE = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')
ROOT = BASE / 'oscillation-20260917'
os.environ['MPLCONFIGDIR'] = str(ROOT / 'mpl-cache')
sys.path.insert(0, str(BASE / 'plot-deps-osc-20260917'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from oscillation_metrics import load_trace

def load_row(row):
    path = Path(row['run_path'])
    m = json.loads((path / 'manifest.json').read_text())
    meta, records = load_trace(path)
    mapping = m['environment']['nic_id_to_name']
    order = [next(j for j in (0, 1) if mapping[str(records[0]['dev' + str(j)])] == name)
             for name in ('erdma_0', 'erdma_1')]
    start = m['measurement_start_ns']
    return meta, records, order, start


def figure(rows, family, profile, left, right, output):
    fig, axes = plt.subplots(4, 2, figsize=(13, 10), sharex=True)
    colors = ('#1767b0', '#de7423')
    for column, policy in enumerate(('free', 'equal')):
        row = next(r for r in rows if r['variant'] == family + '-' + policy and r['profile'] == profile and r['repeat'] == 2)
        meta, records, order, origin = load_row(row)
        normal = [r for r in records if r['mode'] == 1 and left <= (r['ns'] - origin) / 1e9 <= right]
        times = [(r['ns'] - origin) / 1e9 for r in normal]
        title = f"{family}-{policy} | {profile} | repeat 2"
        axes[0, column].set_title(title, fontsize=11)
        for name, j, color in zip(('Rail0', 'Rail1'), order, colors):
            axes[0, column].plot(times, [r['weight' + str(j)] for r in normal], lw=.55, color=color, label=name)
            axes[2, column].plot(times, [r['inflight' + str(j)] / 1048576 for r in normal], lw=.55, color=color, label=name)
            axes[3, column].plot(times, [r['bandwidth' + str(j)] * 8e-9 for r in normal], lw=.55, color=color, label=name)
        probes = [(r['ns'] - origin) / 1e9 for r in records if r['mode'] == 2 and left <= (r['ns'] - origin) / 1e9 <= right]
        axes[0, column].scatter(probes, [1.025] * len(probes), marker='|', s=12, color='#555555', label='probe')
        axes[0, column].set_ylim(-.03, 1.05)
        axes[0, column].set_ylabel('Shadow weight' if meta['fixed_equal'] else 'Consumed weight')
        axes[0, column].legend(loc='lower right', fontsize=8, ncol=3)
        all_records = [r for r in records if left <= (r['ns'] - origin) / 1e9 <= right]
        axes[1, column].plot([(r['ns'] - origin) / 1e9 for r in all_records],
            [r['assigned' + str(order[0])] / r['total_bytes'] for r in all_records],
            lw=.4, color='#94b2c7', label='per request')
        grid = [w for w in row['grid_250ms'] if left <= w['time'] <= right]
        for key, label, color in (('allocation', 'allocation / 250ms', '#174b74'),
                                   ('post', 'post / 250ms', '#aa398b')):
            axes[1, column].plot([w['time'] for w in grid],
                [w[key] if w[key] is not None else float('nan') for w in grid], lw=1.1, color=color, label=label)
        axes[1, column].set_ylim(-.03, 1.03)
        axes[1, column].set_ylabel('Rail0 byte share')
        axes[1, column].legend(loc='lower right', fontsize=7)
        axes[2, column].set_ylabel('Inflight MiB (symlog)')
        axes[2, column].set_yscale('symlog', linthresh=.25)
        axes[3, column].set_ylabel('Estimated BW Gbps (log)')
        axes[3, column].set_yscale('log')
        axes[3, column].set_xlabel('Seconds from measurement start')
        for axis in axes[:, column]:
            axis.set_xlim(left, right); axis.grid(alpha=.2)
            if left < 10:
                axis.axvspan(0, min(10, right), color='#aaaaaa', alpha=.13)
    fig.suptitle('Uniform planned input: original computed weights and applied allocation', fontsize=13)
    fig.tight_layout(rect=(0, .01, 1, .965))
    fig.savefig(output, dpi=110)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'figures')
    args = parser.parse_args()
    report = json.loads((ROOT / 'formal-oscillation-analysis.json').read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    plt.rcParams.update({'path.simplify': False, 'font.family': 'DejaVu Sans'})
    files = []
    for family in ('native', 'symmetric'):
        for profile in ('20pct', '60pct'):
            for label, left, right in (('full', 0, 60), ('zoom', 10, 12)):
                name = f'{family}-{profile}-r2-{label}.png'
                figure(report['runs'], family, profile, left, right, args.output / name)
                files.append(name)
                print('O_FIGURE', name, flush=True)
    (args.output / 'plot-manifest.json').write_text(json.dumps(dict(
        matplotlib=matplotlib.__version__, files=files, repeat=2,
        zoom_seconds=[10, 12], scope='preset representative views, not selected for largest effect'), indent=2))
    print('OSCILLATION_FIGURES_COMPLETE', len(files), flush=True)


if __name__ == '__main__':
    main()
