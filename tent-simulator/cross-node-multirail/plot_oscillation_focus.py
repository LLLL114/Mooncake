#!/usr/bin/env python3
"""Readable report views of the preset native/60%/repeat2 data; no new selection."""
import json

from plot_oscillation import ROOT, load_row, plt


def main():
    report = json.loads((ROOT / 'formal-oscillation-analysis.json').read_text())
    output = ROOT / 'figures-focus'; output.mkdir(exist_ok=False)
    plt.rcParams.update({'path.simplify': False, 'font.family': 'DejaVu Sans'})
    for label, left, right in (('full', 0, 60), ('zoom', 10, 12)):
        fig, axes = plt.subplots(2, 2, figsize=(12, 5.5), sharex=True)
        for col, policy in enumerate(('free', 'equal')):
            row = next(r for r in report['runs'] if r['variant'] == 'native-' + policy
                       and r['profile'] == '60pct' and r['repeat'] == 2)
            meta, records, order, origin = load_row(row)
            normal = [r for r in records if r['mode'] == 1 and left <= (r['ns'] - origin) / 1e9 <= right]
            j = order[0]
            times = [(r['ns'] - origin) / 1e9 for r in normal]
            axes[0, col].plot(times, [r['weight' + str(j)] for r in normal], color='#1767b0', lw=.55)
            axes[0, col].set_title('Original free allocation' if policy == 'free' else 'Fixed 50/50 allocation', fontsize=11)
            axes[0, col].set_ylabel('Rail0 consumed weight' if policy == 'free' else 'Rail0 shadow weight')
            all_records = [r for r in records if left <= (r['ns'] - origin) / 1e9 <= right]
            axes[1, col].plot([(r['ns'] - origin) / 1e9 for r in all_records],
                [r['assigned' + str(j)] / r['total_bytes'] for r in all_records],
                color='#96b3c8', lw=.45, label='each request')
            grid = [w for w in row['grid_250ms'] if left <= w['time'] <= right]
            axes[1, col].plot([w['time'] for w in grid],
                [w['allocation'] if w['allocation'] is not None else float('nan') for w in grid],
                color='#b95116', lw=1.15, label='250ms byte share')
            axes[1, col].set_ylabel('Rail0 allocated share')
            axes[1, col].set_xlabel('Seconds from measurement start')
            axes[1, col].legend(fontsize=8, loc='lower right')
            for axis in axes[:, col]:
                axis.set_ylim(-.025, 1.025); axis.set_xlim(left, right); axis.grid(alpha=.2)
                if left < 10:
                    axis.axvspan(0, 10, color='#999999', alpha=.13)
        fig.suptitle('Real cross-node TENT | constant 60% reference input | preset repeat 2', fontsize=12)
        fig.tight_layout(rect=(0, .01, 1, .94))
        name = f'native-60pct-r2-{label}.png'
        fig.savefig(output / name, dpi=95)
        plt.close(fig)
        print('O_FOCUS_FIGURE', name, (output / name).stat().st_size, flush=True)
    (output / 'manifest.json').write_text(json.dumps(dict(source='preset native-60pct-r2',
        panels='same raw weights and allocation as full protocol figures; fewer panels for readability',
        windows_seconds=[[0, 60], [10, 12]], weight='Rail0 only; Rail1 complements it'), indent=2))


if __name__ == '__main__':
    main()
