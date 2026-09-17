#!/usr/bin/env python3
"""Server-only scientific curves; fixed repeat2 and time windows, shared axes."""
from collections import defaultdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys

from run_candidates import BASE, ROOT, POLICIES
from candidate_metrics import load_trace

os.environ['MPLCONFIGDIR'] = str(ROOT / 'mpl-cache')
sys.path.insert(0, str(BASE / 'plot-deps-osc-20260917'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def envelope(axis, t, v, color, label, alpha):
    buckets = defaultdict(list)
    for x, y in zip(t, v):
        buckets[int(x / .5)].append(y)
    keys = sorted(buckets)
    x = [(k + .5) * .5 for k in keys]
    axis.fill_between(x, [min(buckets[k]) for k in keys], [max(buckets[k]) for k in keys],
                      color=color, alpha=alpha, linewidth=0)
    axis.plot(x, [statistics.mean(buckets[k]) for k in keys], color=color, lw=.9, label=label)


def main():
    report = json.loads((ROOT / 'candidate-analysis.json').read_text())
    out = ROOT / 'figures'; out.mkdir(exist_ok=False)
    plt.rcParams.update({'path.simplify': False, 'font.family': 'DejaVu Sans', 'svg.fonttype': 'none'})
    colors = ('#1767b0', '#c87510', '#168250', '#8541ae')
    manifests = []
    for profile in ('20pct', '60pct'):
        fig, axes = plt.subplots(4, 2, figsize=(12, 11), sharey=True)
        allocfig, allocaxes = plt.subplots(4, 1, figsize=(12, 8), sharex=True, sharey=True)
        for index, policy in enumerate(POLICIES):
            row = next(r for r in report['runs'] if r['variant'] == policy and r['profile'] == profile and r['repeat'] == 2)
            path = Path(row['run_path'])
            manifest = json.loads((path / 'manifest.json').read_text())
            _, records = load_trace(path)
            normal = [r for r in records if r['mode'] == 1]
            if manifest['environment']['nic_id_to_name'][str(normal[0]['dev0'])] != 'erdma_0':
                raise ValueError('figure rail ordering differs')
            t = [(r['ns'] - manifest['measurement_start_ns']) / 1e9 for r in normal]
            actual = [r['weight0'] for r in normal]
            raw = []
            for r in normal:
                v = [1 / (r['score' + str(j)] + 1e-12) for j in (0, 1)]
                raw.append(v[0] / sum(v))
            envelope(axes[index, 0], t, raw, '#777777', 'raw score weight: mean + min/max', .12)
            envelope(axes[index, 0], t, actual, colors[index], 'consumed weight: mean + min/max', .22)
            zoom = [i for i, x in enumerate(t) if 10 <= x <= 12]
            axes[index, 1].plot([t[i] for i in zoom], [raw[i] for i in zoom], color='#aaaaaa', lw=.55, label='raw score weight')
            axes[index, 1].plot([t[i] for i in zoom], [actual[i] for i in zoom], color=colors[index], lw=.8, label='consumed weight')
            axes[index, 0].set_xlim(0, 60); axes[index, 0].axvspan(0, 10, color='#999999', alpha=.1)
            axes[index, 1].set_xlim(10, 12)
            for j, axis in enumerate(axes[index]):
                axis.set_ylim(-.025, 1.025); axis.grid(alpha=.2)
                axis.set_title(policy + (' | 0.5s envelope' if j == 0 else ' | individual normal decisions'), fontsize=10)
                axis.set_ylabel('Rail0 weight'); axis.set_xlabel('Seconds')
                axis.legend(fontsize=7, loc='lower right')
            grid = row['grid_250ms']
            allocaxes[index].plot([r['time'] for r in grid], [r['allocation'] for r in grid], color=colors[index], lw=.9)
            allocaxes[index].set_title(policy, fontsize=10); allocaxes[index].set_ylim(-.025, 1.025)
            allocaxes[index].set_ylabel('Rail0 byte share'); allocaxes[index].grid(alpha=.2)
        fig.suptitle(f'Real TENT | constant {profile} of reference input | preset repeat 2\nEach row is a separate real run; gray=raw score, color=consumed weight', fontsize=12)
        fig.tight_layout(rect=(0, 0, 1, .94))
        allocfig.suptitle(f'Actual allocation byte share in 250ms windows | {profile} | preset repeat 2', fontsize=12)
        allocaxes[-1].set_xlabel('Seconds'); allocaxes[-1].set_xlim(0, 60)
        allocfig.tight_layout(rect=(0, 0, 1, .96))
        for f, name in ((fig, 'weights-' + profile), (allocfig, 'allocation-' + profile)):
            f.savefig(out / (name + '.png'), dpi=140)
            path = out / (name + '.svg'); f.savefig(path, format='svg')
            raw = path.read_bytes(); (out / (name + '.svg.gz')).write_bytes(gzip.compress(raw))
            manifests.append(dict(file=path.name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
            plt.close(f)
    (out / 'manifest.json').write_text(json.dumps(dict(figures=manifests,
        selection='repeat2, full0–60s with 0.5s min/max/mean, raw zoom10–12s',
        scope='real independent closed-loop runs, not same feedback replay'), indent=2))
    print('CAND_FIGURES_COMPLETE', json.dumps(manifests), flush=True)


if __name__ == '__main__':
    main()
