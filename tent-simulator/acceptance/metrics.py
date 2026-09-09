#!/usr/bin/env python3
"""Fixed-time-window metrics, separate completion and planned-arrival cohorts."""

import sys as _sys
_sys.dont_write_bytecode = True
from pathlib import Path as _BootstrapPath
_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1] / "scripts"))
from experiment_paths import OUTPUT_ROOT
import math,pathlib,sys
ROOT=pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0,str(OUTPUT_ROOT/'build/plot-deps'))
import numpy as np
DTYPE=np.dtype([('planned','<u8'),('submitted','<u8'),('finished','<u8'),('allocation_ns','<u8'),('weight0','<f8'),('share0','<f8'),('first_rail','<u8')])
def q(x,p):
    x=np.asarray(x)
    if not len(x):return None
    k=max(0,math.ceil(len(x)*p)-1)
    return float(np.partition(x,k)[k])
def dispersion(x):
    x=np.asarray([v for v in x if v is not None],dtype=float)
    if not len(x):return {'mean':None,'sd':None,'cv':None,'p95_p5':None,'n':0}
    mean=float(x.mean());sd=float(x.std())
    return {'mean':mean,'sd':sd,'cv':sd/mean if mean else None,'p95_p5':q(x,.95)-q(x,.05),'n':len(x)}
def summarize(data,path,capacity):
    c=data['config'];a=np.memmap(path,dtype=DTYPE,mode='r');n=len(a)
    assert n==data['requests_completed'] and np.all(a['planned']<=a['submitted']) and np.all(a['submitted']<=a['finished'])
    assert np.all(a['finished']>0) and np.all((a['share0']>=0)&(a['share0']<=1))
    lo=c['measurement_start_ns'];hi=c['measurement_stop_ns'];seconds=(hi-lo)/1e9
    select=(a['planned']>=lo)&(a['planned']<hi);b=a[select];lat=(b['finished']-b['planned'])/1000;service=(b['finished']-b['submitted'])/1000
    gap=c.get('arrival_interval_ns',0);size=c['request_bytes'];done=np.asarray(data['done_50ms'],dtype=float);assigned=np.asarray(data['assigned_50ms'],dtype=float)
    windows=[]
    for width in [50000000,250000000,1000000000]:
        for start in range(lo,hi-width+1,width):
            end=start+width;i=np.searchsorted(a['planned'],start);j=np.searchsorted(a['planned'],end);z=a[i:j]
            values=(z['finished']-z['planned'])/1000
            counts=done[start//50000000:end//50000000].sum(axis=0);alloc=assigned[start//50000000:end//50000000].sum(axis=0)
            rates=counts*8/width;active=[k for k in [0,1] if c.get('rail_mask',3)&(1<<k)]
            utilization=[rates[k]/capacity[str(1<<k)] for k in active]
            den=sum(v*v for v in utilization);jain=sum(utilization)**2/(len(active)*den) if den else None
            imbalance=(max(utilization)-min(utilization))/sum(utilization) if sum(utilization) else None
            windows.append({'start_ns':start,'width_ns':width,'requests':len(values),'p99_us':q(values,.99) if len(values)>=500 else None,'goodput_gbps':float(rates.sum()),'rail0_gbps':float(rates[0]),'rail1_gbps':float(rates[1]),'assigned_share0':float(alloc[0]/sum(alloc)) if sum(alloc) else None,'jain':jain,'imbalance':imbalance})
    primary=[w for w in windows if w['width_ns']==250000000];shares=np.array([w['assigned_share0'] for w in primary if w['assigned_share0'] is not None]);wc=data['weight_changes']
    rates=[float(done[lo//50000000:hi//50000000,k].sum()*8/(hi-lo)) for k in [0,1]]
    variation=np.abs(np.diff(b['share0']));first=np.count_nonzero(np.diff(b['first_rail']))
    row={'size':size,'topology':c['topology'],'mask':c.get('rail_mask',3),'load':c.get('offered_fraction',0),'seed':c['seed'],'kind':c['kind'],'measurement_s':seconds,'requests_measured':len(b),'goodput_gbps':sum(rates),'rail0_gbps':rates[0],'rail1_gbps':rates[1],'capacity_fraction':sum(rates)/capacity[str(c.get('rail_mask',3))],'offered_gbps':size*8/gap if gap else None,'p50_us':q(lat,.5),'p99_us':q(lat,.99),'p999_us':q(lat,.999),'service_p99_us':q(service,.99),'allocation_mean_ns':float(b['allocation_ns'].mean()),'allocation_p99_ns':q(b['allocation_ns'],.99),'allocation_p999_ns':q(b['allocation_ns'],.999),'allocation_calls_per_request':1 if data['aggregate_path'] else data['split_count'],'throughput':dispersion([w['goodput_gbps'] for w in primary]),'p99_windows':dispersion([w['p99_us'] for w in primary]),'allocation_share':dispersion(shares),'window_jain':dispersion([w['jain'] for w in primary]),'window_imbalance':dispersion([w['imbalance'] for w in primary]),'weight_decisions':wc['samples'],'weight_event_hz':[v/seconds for v in wc['events']],'weight_events_per_10k':[v*10000/max(1,wc['samples']-1) for v in wc['events']],'weight_reversal_hz':[v/seconds for v in wc['reversals']],'weight_tv_per_s':wc['tv']/seconds,'weight_mean_step':wc['tv']/max(1,wc['samples']-1),'request_assignment_step':float(variation.mean()) if len(variation) else 0,'request_assignment_events_per_10k':float(np.count_nonzero(variation>.001))*10000/max(1,len(variation)),'first_slice_switches_per_10k':first*10000/max(1,len(b)-1),'window_assignment_tv_per_s':float(np.abs(np.diff(shares)).sum())/seconds,'latency_over_1s_fraction':float(np.mean(lat>1e6)),'max_injection_lateness_us':data['max_injection_lateness_ns']/1000,'pending_at_end':0,'quota_inflight_at_end':sum(s['inflight'] for s in data['final_stats']),'actual_flow_migration':None,'retry_migration':None,'fault_settle_ms':None,'split_count':data['split_count'],'aggregate_path':data['aggregate_path']}
    return row,windows
