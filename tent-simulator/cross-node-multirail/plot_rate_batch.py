#!/usr/bin/env python3
"""RB server-only figures, fixed repeat2, common axes and original windows."""
from collections import defaultdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
from run_rate_batch import BASE, ROOT, POLICIES
from rate_batch_metrics import load_trace

os.environ['MPLCONFIGDIR'] = str(ROOT / 'mpl-cache')
sys.path.insert(0,str(BASE / 'plot-deps-osc-20260917'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    report = json.loads((ROOT / 'formal-analysis.json').read_text())
    output = ROOT / 'figures'; output.mkdir(exist_ok=False)
    plt.rcParams.update({'font.family':'DejaVu Sans','svg.fonttype':'none'})
    colors = ('#1767b0','#8b6c42','#238c59','#b65b16','#7148ad','#bb3b65')
    files = []
    for profile in ('20pct','60pct'):
        display = ('reference', *POLICIES)
        fig,axes = plt.subplots(7,2,figsize=(12,16),sharey=True)
        allocation,aa = plt.subplots(7,1,figsize=(12,12),sharex=True,sharey=True)
        for k,policy in enumerate(display):
            row = next(r for r in report['runs'] if r['variant']==policy and r['profile']==profile and r['repeat']==2)
            root = Path(row['run_path']); m = json.loads((root/'manifest.json').read_text())
            _,records = load_trace(root); normal = [r for r in records if r['mode']==1]
            if m['environment']['nic_id_to_name'][str(normal[0]['dev0'])]!='erdma_0':
                raise ValueError('rail ordering changed')
            t = [(r['ns']-m['measurement_start_ns'])/1e9 for r in normal]
            values = [r['weight0'] for r in normal]
            buckets = defaultdict(list)
            for x,y in zip(t,values):
                buckets[int(x/.5)].append(y)
            keys = sorted(buckets); x=[(b+.5)*.5 for b in keys]
            color = '#555555' if k == 0 else colors[k-1]
            axes[k,0].fill_between(x,[min(buckets[b]) for b in keys],[max(buckets[b]) for b in keys],color=color,alpha=.22,linewidth=0,label='0.5s min-max')
            axes[k,0].plot(x,[statistics.mean(buckets[b]) for b in keys],color=color,lw=.9,label='0.5s mean')
            zoom = [i for i,x in enumerate(t) if 10<=x<=12]
            axes[k,1].plot([t[i] for i in zoom],[values[i] for i in zoom],color=color,lw=.65,label='normal decisions')
            axes[k,0].set_xlim(0,60); axes[k,0].axvspan(0,10,color='#aaaaaa',alpha=.12)
            axes[k,1].set_xlim(10,12)
            for j,axis in enumerate(axes[k]):
                axis.set_ylim(-.025,1.025); axis.grid(alpha=.2)
                axis.set_title(policy+(' | 0.5s envelope' if j==0 else ' | 10-12s decisions'),fontsize=10)
                axis.set_ylabel('Rail0 target weight'); axis.set_xlabel('Seconds')
                axis.legend(loc='lower right',fontsize=7)
            grid=row['grid_250ms']
            aa[k].plot([v['time'] for v in grid],[v['allocation'] for v in grid],color=color,lw=.8)
            aa[k].set_ylim(-.025,1.025); aa[k].grid(alpha=.2); aa[k].set_title(policy,fontsize=10)
            aa[k].set_ylabel('Rail0 byte share')
        fig.suptitle(f'Real TENT | {profile} of same-day D0 reference | preset repeat 2\nInverse-score weight or continuous batch target; actual integer allocation shown separately',fontsize=11)
        fig.tight_layout(rect=(0,0,1,.955))
        allocation.suptitle(f'Actual allocation in 250ms windows | {profile} | preset repeat 2',fontsize=11)
        aa[-1].set_xlabel('Seconds'); aa[-1].set_xlim(0,60)
        allocation.tight_layout(rect=(0,0,1,.965))
        for image,name in ((fig,'weights-'+profile),(allocation,'allocation-'+profile)):
            image.savefig(output/(name+'.png'),dpi=130)
            svg=output/(name+'.svg'); image.savefig(svg,format='svg'); plt.close(image)
            raw=svg.read_bytes(); (output/(name+'.svg.gz')).write_bytes(gzip.compress(raw))
            files.append(dict(file=svg.name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
    (output/'manifest.json').write_text(json.dumps(dict(figures=files,selection='repeat2, full0-60s envelope, zoom10-12s'),indent=2))
    print('RB_FIGURES_COMPLETE',json.dumps(files),flush=True)


if __name__=='__main__':
    main()
