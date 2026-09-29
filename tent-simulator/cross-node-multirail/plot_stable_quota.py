#!/usr/bin/env python3
"""SSH-only SQ figures; preset repeat 2, fixed axes, envelope is not a CI."""
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
from run_stable_quota import BASE, ROOT, POLICIES
from oscillation_metrics import load_trace

os.environ['MPLCONFIGDIR'] = str(ROOT/'mpl-cache')
sys.path.insert(0,str(BASE/'plot-deps-osc-20260917'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    report = json.loads((ROOT/'formal-analysis.json').read_text())
    output = ROOT/'figures'; output.mkdir(exist_ok=False)
    plt.rcParams.update({'font.family':'DejaVu Sans','svg.fonttype':'none'})
    files = []
    for profile, rate in (('20pct',225),('60pct',675)):
        fig, axes = plt.subplots(3,2,figsize=(12,8),sharey=True)
        allocation, aa = plt.subplots(3,1,figsize=(12,7),sharex=True,sharey=True)
        for k, policy in enumerate(POLICIES):
            row = next(r for r in report['runs'] if r['variant']==policy and r['profile']==profile and r['repeat']==2)
            root = Path(row['run_path']); m = json.loads((root/'manifest.json').read_text())
            _, records = load_trace(root); normal = [r for r in records if r['mode']==1]
            t = [(r['ns']-m['measurement_start_ns'])/1e9 for r in normal]
            values = [r['weight0'] for r in normal]
            buckets = defaultdict(list)
            for x,y in zip(t,values):
                buckets[int(x/.5)].append(y)
            keys = sorted(buckets); x = [(b+.5)*.5 for b in keys]
            color = ('#555555','#238c59','#1767b0')[k]
            axes[k,0].fill_between(x,[min(buckets[b]) for b in keys],[max(buckets[b]) for b in keys],color=color,alpha=.22,linewidth=0,label='0.5s min-max')
            axes[k,0].plot(x,[statistics.mean(buckets[b]) for b in keys],color=color,lw=.9,label='0.5s mean')
            zoom = [i for i,x in enumerate(t) if 10<=x<=12]
            axes[k,1].plot([t[i] for i in zoom],[values[i] for i in zoom],color=color,lw=.65,label='normal decisions')
            axes[k,0].set_xlim(0,60); axes[k,0].axvspan(0,10,color='#aaaaaa',alpha=.12)
            axes[k,1].set_xlim(10,12)
            for j, axis in enumerate(axes[k]):
                axis.set_ylim(-.025,1.025); axis.grid(alpha=.2)
                axis.set_title(policy+(' | envelope' if j==0 else ' | zoom'),fontsize=10)
                axis.set_ylabel('Rail0 target weight'); axis.set_xlabel('Seconds')
                axis.legend(loc='lower right',fontsize=7)
            grid = row['grid_250ms']
            aa[k].plot([v['time'] for v in grid],[v['allocation'] for v in grid],color=color,lw=.8)
            aa[k].set_ylim(-.025,1.025); aa[k].grid(alpha=.2); aa[k].set_title(policy)
            aa[k].set_ylabel('Rail0 byte share')
        fig.suptitle(f'Real TENT | {rate} requests/s, 1 MiB each | preset repeat 2\nOriginal inverse-score weight vs consumed stable quota target',fontsize=11)
        fig.tight_layout(rect=(0,0,1,.93))
        allocation.suptitle(f'Actual allocation in 250ms windows | {rate} requests/s | repeat 2',fontsize=11)
        aa[-1].set_xlabel('Seconds'); aa[-1].set_xlim(0,60)
        allocation.tight_layout(rect=(0,0,1,.94))
        for plot, name in ((fig,'weights-'+profile),(allocation,'allocation-'+profile)):
            path = output/(name+'.svg'); plot.savefig(path,format='svg'); plt.close(plot)
            raw = path.read_bytes()
            files.append(dict(file=path.name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
    (output/'manifest.json').write_text(json.dumps(dict(figures=files,selection='repeat2, full0-60, zoom10-12'),indent=2))
    print('SQ_FIGURES_COMPLETE',json.dumps(files),flush=True)


if __name__ == '__main__':
    main()
