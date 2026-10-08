#!/usr/bin/env python3
"""Offline static figures; one dot per complete run, no confidence bands."""
import json
from pathlib import Path
import statistics
import sys
from collections import defaultdict
from oscillation_metrics import load_trace

from run_reference_rail_controls import ROOT
from run_oscillation import BASE


def main():
    sys.path.insert(0,str(BASE/'plot-deps-osc-20260917'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    rows=json.loads((ROOT/'analysis.json').read_text())['runs']
    out=ROOT/'figures';out.mkdir(exist_ok=True)
    modes=('D0','S0','S1');labels=('Dual Rail','Rail 0 only','Rail 1 only')
    colors=('#1768ac','#c95c24','#268368')
    fig,axes=plt.subplots(2,2,figsize=(10,7),layout='constrained')
    for col,profile in enumerate(('225rps','675rps')):
        for line,(metric,label) in enumerate((('p99_ms','End-to-end P99 (ms)'),('goodput_gbps','Goodput (Gbps)'))):
            ax=axes[line,col]
            for i,mode in enumerate(modes):
                values=[r['performance'][metric] for r in sorted(rows,key=lambda r:r['repeat']) if r['mode']==mode and r['profile']==profile]
                ax.scatter([i-.07,i,i+.07],values,color=colors[i],s=42,zorder=3)
                median=statistics.median(values);ax.plot([i-.18,i+.18],[median,median],color='black',lw=2)
            ax.set(xticks=range(3),xticklabels=labels,ylabel=label,title=profile.replace('rps',' requests/s'))
            ax.grid(axis='y',alpha=.2);ax.set_ylim(bottom=0,top=ax.get_ylim()[1]*1.10)
    fig.suptitle('Original routing: matched offered load, 6 lanes\nDots: individual runs; black bars: medians (3 runs each)',fontsize=12)
    for ext in ('svg','png'):fig.savefig(out/('reference-rail-controls.'+ext),dpi=160)
    plt.close(fig)
    pv=json.loads((BASE/'provider-diagnostic-20261008/overhead-analysis.json').read_text())
    fig,axes=plt.subplots(1,2,figsize=(9,3.6),layout='constrained')
    for ax,key,title,limit in zip(axes,('goodput_ratio','p99_ratio'),('Goodput ratio','P99 ratio'),(.95,1.20)):
        values=[p[key] for p in sorted(pv['pairs'],key=lambda p:p['repeat'])]
        ax.plot([1,2,3],values,'o-',color='#1768ac',label='Provider / reference')
        ax.axhline(1,color='gray',lw=1);ax.axhline(limit,color='#c95c24',ls='--',label=f'Gate: {limit:.2f}')
        ax.set(xticks=[1,2,3],xlabel='Paired repeat',ylabel=title,title=title)
        ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.suptitle('Observation gate failed: all three paired P99 ratios exceed 1.20',fontsize=11)
    for ext in ('svg','png'):fig.savefig(out/('provider-overhead-gate.'+ext),dpi=160)
    plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(11,6),layout='constrained')
    for col,profile in enumerate(('225rps','675rps')):
        for row in sorted(rows,key=lambda r:r['repeat']):
            if row['mode']!='D0' or row['profile']!=profile: continue
            root=Path(row['run_path']);m=json.loads((root/'manifest.json').read_text())
            _,trace=load_trace(root);bins=defaultdict(lambda:[[],0,0])
            for point in trace:
                index=(point['ns']-m['measurement_start_ns'])//250000000
                if not 0<=index<240: continue
                b=bins[index]
                if point['mode']==1:b[0].append(point['weight0'])
                b[1]+=point['assigned0'];b[2]+=point['total_bytes']
            for line in range(2):
                points=[(i*.25+.125,statistics.median(b[0]) if line==0 else b[1]/b[2])
                        for i,b in sorted(bins.items()) if b[0] and b[2]]
                axes[line,col].plot([p[0] for p in points],[p[1] for p in points],
                    lw=1,color=colors[row['repeat']-1],label=f"Repeat {row['repeat']}")
        for line in range(2):
            ax=axes[line,col];ax.set(xlabel='Measurement time (s)',ylim=(0,1),
                ylabel='Rail 0 weight (bin median)' if line==0 else 'Rail 0 assigned byte share',title=profile)
            ax.axvline(10,color='gray',ls=':',lw=1);ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.suptitle('Original dual-Rail routing: 250ms display bins, no confidence band\nRaw oscillation metrics use 10–60s; allocation includes probe decisions',fontsize=11)
    for ext in ('svg','png'):fig.savefig(out/('original-weight-allocation.'+ext),dpi=160)
    plt.close(fig)
    print('RC_FIGURES_COMPLETE',out)


if __name__=='__main__':main()
