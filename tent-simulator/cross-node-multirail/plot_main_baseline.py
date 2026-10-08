#!/usr/bin/env python3
"""Current-main figures; run only after all measurement traffic has ended."""
from collections import defaultdict
import json
from pathlib import Path
import statistics
import sys
from build_main_selection import ROOT,BASE
from main_baseline_metrics import load_trace


def main():
    sys.path.insert(0,str(BASE/'plot-deps-osc-20260917'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from PIL import Image
    analysis=json.loads((ROOT/'analysis.json').read_text());rows=analysis['runs']
    gates={g['profile']:g['observation_gate_pass'] for g in analysis['groups']}
    out=ROOT/'figures';out.mkdir(exist_ok=True)
    profiles=('225rps','675rps','saturated');colors=('#1768ac','#c95c24','#268368')
    def save(fig,name):
        fig.savefig(out/(name+'.svg'))
        fig.savefig(out/(name+'.png'),dpi=90)
        im=Image.open(out/(name+'.png')).convert('RGB');im.thumbnail((1100,600))
        im.quantize(colors=64).save(out/(name+'-preview.png'),optimize=True)
        plt.close(fig)
    fig,axes=plt.subplots(2,3,figsize=(13,6),layout='constrained')
    for col,profile in enumerate(profiles):
        for run in sorted(rows,key=lambda r:r['repeat']):
            if run['variant']!='trace' or run['profile']!=profile:continue
            root=Path(run['run_path']);m=json.loads((root/'manifest.json').read_text())
            _,trace=load_trace(root);j=str(run['audit']['rail0_column'])
            bins=defaultdict(lambda:[[],0,0])
            for p in trace:
                index=(p['ns']-m['measurement_start_ns'])//250000000
                if not 0<=index<240:continue
                b=bins[index]
                if p['mode']==1:b[0].append(p['weight'+j])
                b[1]+=p['assigned'+j];b[2]+=p['total_bytes']
            for line in range(2):
                points=[(i*.25+.125,statistics.median(b[0]) if line==0 else b[1]/b[2])
                        for i,b in sorted(bins.items()) if b[0] and b[2]]
                axes[line,col].plot([p[0] for p in points],[p[1] for p in points],lw=1,
                    color=colors[run['repeat']-1],label=f"Repeat {run['repeat']}")
        for line in range(2):
            ax=axes[line,col];ax.set(xlabel='Measurement time (s)',ylim=(0,1),title=profile+('' if gates[profile] else '\nObservation gate failed'),
                ylabel='Rail 0 weight: bin median' if line==0 else 'Rail 0 assigned byte share')
            ax.axvline(10,color='gray',ls=':',lw=1);ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.suptitle('Current main original scheduling: decision observation\n250ms display bins; raw oscillation metrics use 10-60s; no confidence bands',fontsize=11)
    save(fig,'main-weight-allocation')
    fig,axes=plt.subplots(1,2,figsize=(10,4),layout='constrained')
    for ax,key,label in zip(axes,('p99_ms','goodput_gbps'),('End-to-end P99 (ms)','Goodput (Gbps)')):
        maximum=0
        for i,profile in enumerate(profiles):
            selected=sorted([r for r in rows if r['variant']=='stock' and r['profile']==profile],key=lambda r:r['repeat'])
            values=[r['performance'][key] for r in selected];maximum=max(maximum,max(values))
            ax.scatter([i-.07,i,i+.07],values,color=colors,s=36,zorder=3)
            median=statistics.median(values);ax.plot([i-.18,i+.18],[median,median],color='black',lw=2)
        ax.set(xticks=range(3),xticklabels=profiles,ylabel=label,ylim=(0,maximum*1.15));ax.grid(axis='y',alpha=.2)
    fig.suptitle('Current main stock library: performance baseline\nEach dot is one run; black bars are medians of 3 runs',fontsize=11)
    save(fig,'main-stock-performance')
    plot_raw_window()
    print('MAIN_FIGURES_COMPLETE',out)


def plot_raw_window():
    sys.path.insert(0,str(BASE/'plot-deps-osc-20260917'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from PIL import Image
    a=json.loads((ROOT/'analysis.json').read_text());out=ROOT/'figures'
    gates={g['profile']:g['observation_gate_pass'] for g in a['groups']}
    colors=('#1768ac','#c95c24','#268368')
    fig,axes=plt.subplots(2,3,figsize=(13,6),layout='constrained')
    for col,profile in enumerate(('225rps','675rps','saturated')):
        for run in sorted(a['runs'],key=lambda r:r['repeat']):
            if run['variant']!='trace' or run['profile']!=profile:continue
            root=Path(run['run_path']);m=json.loads((root/'manifest.json').read_text())
            _,trace=load_trace(root);j=str(run['audit']['rail0_column'])
            selected=[r for r in trace if r['mode']==1 and 20000000000<=r['ns']-m['measurement_start_ns']<20200000000]
            times=[(r['ns']-m['measurement_start_ns']-20000000000)/1e6 for r in selected]
            for line in range(2):
                values=[r['weight'+j] if line==0 else r['assigned'+j]/r['total_bytes'] for r in selected]
                axes[line,col].plot(times,values,lw=.9,color=colors[run['repeat']-1],label=f"Repeat {run['repeat']}")
        for line in range(2):
            ax=axes[line,col];ax.set(xlim=(0,200),ylim=(0,1),xlabel='Time after 20s (ms)',
                ylabel='Rail 0 raw weight' if line==0 else 'Rail 0 per-request byte share',
                title=profile+('' if gates[profile] else '\nObservation gate failed'))
            ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.suptitle('Raw decision detail: the same fixed 20.000-20.200s window for all runs\nNo averaging; lines connect successive normal decisions; full-run metrics remain 10-60s',fontsize=11)
    fig.savefig(out/'main-raw-window.svg');fig.savefig(out/'main-raw-window.png',dpi=90);plt.close(fig)
    im=Image.open(out/'main-raw-window.png').convert('RGB');im.thumbnail((800,420))
    im.quantize(colors=16).save(out/'main-raw-window-qa.png',optimize=True)
    print('MAIN_RAW_WINDOW_COMPLETE',out)


if __name__=='__main__':main()
