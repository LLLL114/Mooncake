#!/usr/bin/env python3
"""SSH-only descriptive timing figures; no hardware-latency attribution claim."""
import hashlib
import json
import os
import sys
from run_poll_diagnostic import ROOT,BASE

os.environ['MPLCONFIGDIR']=str(ROOT/'mpl-cache')
sys.path.insert(0,str(BASE/'plot-deps-osc-20260917'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    report=json.loads((ROOT/'formal-analysis.json').read_text())
    rows=sorted([r for r in report['runs'] if r['variant']=='timed'],key=lambda r:(r['profile'],r['repeat']))
    output=ROOT/'figures';output.mkdir(exist_ok=False)
    plt.rcParams.update({'font.family':'DejaVu Sans','svg.fonttype':'none'})
    labels=[r['profile']+' r'+str(r['repeat']) for r in rows]
    x=list(range(len(rows)));files=[]
    fig,axis=plt.subplots(figsize=(11,5));bottom=[0.]*len(rows)
    for key,label,color in [('injection','Before submit','#6093bc'),('submit_to_enqueue','Submit to enqueue','#cfb15b'),
        ('enqueue_to_post_mark','Enqueue to post mark','#ce7943'),('post_mark_to_cq','Post mark to observed CQ','#8264aa'),
        ('cq_to_caller','CQ processing / caller observation','#6a9c6f')]:
        values=[r['tail']['part_ms'][key]['mean'] for r in rows]
        axis.bar(x,values,bottom=bottom,label=label,color=color)
        bottom=[a+b for a,b in zip(bottom,values)]
    axis.set_xticks(x,labels,rotation=15);axis.set_ylabel('Mean time (ms)')
    axis.set_title('Mean timing chain of the slowest 1% requests\nThese are additive cohort means, not a sum of stage P99 values')
    axis.legend(fontsize=8);axis.grid(axis='y',alpha=.2);fig.tight_layout()
    p=output/'tail-time-chain.svg';fig.savefig(p,format='svg');plt.close(fig);files.append(p)
    fig,axis=plt.subplots(figsize=(11,5));bottom=[0.]*len(rows)
    for key,label,color in [('dense_polling','Frequent short wrapper calls / recent <64 return','#5f9a78'),
        ('large_poll_service_gaps','At least half of lifetime in long CQ service intervals','#ce7943'),('mixed','Mixed / insufficient timing separation','#92959a')]:
        values=[100*r['tail_slow_slices']['categories'].get(key,0)/r['tail_slow_slices']['slices'] if r['tail_slow_slices']['slices'] else 0 for r in rows]
        axis.bar(x,values,bottom=bottom,label=label,color=color);bottom=[a+b for a,b in zip(bottom,values)]
    for i,r in enumerate(rows):
        n=r['tail_slow_slices']['slices'];axis.text(i,102,'n='+str(n) if n else 'N/A',ha='center',fontsize=8)
    axis.set_xticks(x,labels,rotation=15);axis.set_ylim(0,115);axis.set_ylabel('Slow-Slice share (%)')
    axis.set_title('Slices with post-to-CQ >=10ms within the slowest 1% requests\nRdmaCQ::poll wrapper observations; quota-zero may bypass the verbs call')
    axis.legend(fontsize=7,loc='lower left');fig.tight_layout()
    p=output/'tail-poll-service.svg';fig.savefig(p,format='svg');plt.close(fig);files.append(p)
    manifest=dict(figures=[dict(file=p.name,bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in files])
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2));print('PD_FIGURES_COMPLETE',json.dumps(manifest),flush=True)


if __name__=='__main__':main()
