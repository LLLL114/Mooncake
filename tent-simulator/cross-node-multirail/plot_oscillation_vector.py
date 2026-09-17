#!/usr/bin/env python3
"""Compact vector report: full-interval envelope and the preset raw 10–12s view."""
from collections import defaultdict
import gzip
import json
import statistics

from plot_oscillation import ROOT, load_row, plt


def main():
    report = json.loads((ROOT / 'formal-oscillation-analysis.json').read_text())
    output = ROOT / 'figures-vector'; output.mkdir(exist_ok=False)
    plt.rcParams.update({'path.simplify': False, 'font.family': 'DejaVu Sans', 'svg.fonttype': 'none'})
    fig, axes = plt.subplots(2, 2, figsize=(12, 6))
    for col, policy in enumerate(('free', 'equal')):
        row = next(r for r in report['runs'] if r['variant'] == 'native-' + policy
                   and r['profile'] == '60pct' and r['repeat'] == 2)
        meta, records, order, origin = load_row(row)
        normal = [r for r in records if r['mode'] == 1]
        j = order[0]
        buckets = defaultdict(list)
        for r in normal:
            buckets[int((r['ns'] - origin) / 500000000)].append(r['weight' + str(j)])
        bins = sorted(buckets)
        times = [(i + .5) * .5 for i in bins]
        axes[0, col].fill_between(times, [min(buckets[i]) for i in bins],
            [max(buckets[i]) for i in bins], color='#1767b0', alpha=.2, label='0.5s min–max')
        axes[0, col].plot(times, [statistics.mean(buckets[i]) for i in bins],
            color='#1767b0', lw=.8, label='0.5s mean')
        axes[0, col].set_title('Original free allocation' if policy == 'free' else 'Fixed 50/50 allocation', fontsize=12)
        axes[0, col].set_xlim(0, 60); axes[0, col].axvspan(0, 10, color='#aaaaaa', alpha=.15)
        axes[0, col].set_xlabel('Seconds: whole measured interval')
        axes[0, col].legend(fontsize=8, loc='lower right')
        zoom = [r for r in normal if 10 <= (r['ns'] - origin) / 1e9 <= 12]
        axes[1, col].plot([(r['ns'] - origin) / 1e9 for r in zoom],
            [r['weight' + str(j)] for r in zoom], color='#1767b0', lw=.55)
        axes[1, col].set_xlim(10, 12)
        axes[1, col].set_xlabel('Seconds: individual normal decisions, 10–12s')
        for axis in axes[:, col]:
            axis.set_ylim(-.025, 1.025); axis.grid(alpha=.2)
            axis.set_ylabel('Rail0 consumed weight' if policy == 'free' else 'Rail0 shadow weight')
    fig.suptitle('Real cross-node TENT | native priors | constant 60% reference input | preset repeat 2', fontsize=12)
    fig.tight_layout(rect=(0, .01, 1, .94))
    target = output / 'weight-proof-native-60pct-r2.svg'
    fig.savefig(target, format='svg')
    plt.close(fig)
    raw = target.read_bytes()
    (output / (target.name + '.gz')).write_bytes(gzip.compress(raw))
    (output / 'manifest.json').write_text(json.dumps(dict(
        source='native-60pct-r2, same records as original protocol figures',
        top='0.5-second min/max envelope and mean; not a claim that individual decisions follow the mean',
        bottom='unaltered individual normal-decision weights in the preset 10–12s interval',
        shadow='fixed-equal calculation does not drive actual allocation'), indent=2))
    print('O_VECTOR_FIGURE', len(raw), (output / (target.name + '.gz')).stat().st_size, flush=True)


if __name__ == '__main__':
    main()
