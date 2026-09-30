#!/usr/bin/env python3
"""Reuse identical axes and preset repeat selection for TG figures."""
import hashlib
import json
from run_tail_gain import ROOT, POLICIES
import run_stable_quota as runner

runner.ROOT,runner.POLICIES = ROOT,POLICIES
import plot_stable_quota as plot

if __name__=='__main__':
    plot.main()
    report=json.loads((ROOT/'formal-analysis.json').read_text())
    output=ROOT/'figures'; manifest=json.loads((output/'manifest.json').read_text())
    for profile,rps in [('20pct',225),('60pct',675)]:
        fig,axis=plot.plt.subplots(figsize=(10,4))
        for policy,color in zip(POLICIES,('#555555','#238c59','#1767b0')):
            row=next(r for r in report['runs'] if r['variant']==policy and r['profile']==profile and r['repeat']==2)
            windows=row['p99_5s_windows']
            axis.plot([w['time'] for w in windows],[w['p99_ms'] for w in windows],marker='o',ms=3,color=color,label=policy)
        axis.set(xlim=(0,60),ylim=(0,None),xlabel='Seconds',ylabel='End-to-end P99 (ms)',
            title=f'{rps} requests/s | preset repeat 2 | 5s arrival cohorts, descriptive only')
        axis.grid(alpha=.2); axis.legend(); fig.tight_layout()
        path=output/('p99-'+profile+'.svg'); fig.savefig(path,format='svg'); plot.plt.close(fig)
        raw=path.read_bytes(); manifest['figures'].append(dict(file=path.name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print('TG_P99_FIGURES_COMPLETE',flush=True)
