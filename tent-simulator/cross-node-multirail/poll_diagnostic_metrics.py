"""Exact native request/Slice accounting plus conservative poll-gap diagnostics."""
import bisect
from collections import defaultdict
import json
from pathlib import Path
import struct
from oscillation_metrics import read_json,load_trace

COMP=struct.Struct('<14Q4I')
NAMES='request offset bytes enqueue submit poll_begin poll_end previous_begin previous_end drained_begin drained_end loop_begin phase_begin handled worker dev qp reserved'.split()
GAP=struct.Struct('<8Q')
GAP_NAMES='previous_end begin end loop phase inflight dev count'.split()


def clock_offset(meta):
    samples=meta['clock_samples']
    if meta.get('clock')!='CLOCK_REALTIME' or len(samples)<2 or any(not 0<b<=a or not r for b,r,a in samples):
        raise ValueError('invalid clock calibration')
    # If a constant RT-MONO offset exists, it must be in every bracket interval.
    lower=max(r-a for b,r,a in samples);upper=min(r-b for b,r,a in samples)
    if lower>upper or upper-lower>20000: raise ValueError('clock offset changed or calibration too uncertain')
    return dict(offset_ns=(lower+upper)//2,uncertainty_ns=(upper-lower+1)//2,samples=len(samples))


def load(root):
    root=Path(root); meta=read_json(root/'poll-diagnostic.json')
    provider=meta['schema']=='tent-provider-diagnostic-v1'
    if meta['schema'] not in ('tent-poll-diagnostic-v1','tent-provider-diagnostic-v1') or meta['error'] or meta['completion_bytes']!=128 or meta['gap_bytes']!=64:
        raise ValueError('invalid diagnostic metadata')
    mapping=clock_offset(meta);meta['monotonic_mapping']=mapping
    records=defaultdict(list); gaps=defaultdict(list)
    for w in meta['workers']:
        if Path(w['completion_file']).name!=w['completion_file'] or Path(w['gap_file']).name!=w['gap_file']:
            raise ValueError('invalid diagnostic filename')
        raw=(root/w['completion_file']).read_bytes()
        if len(raw)!=w['completions']*COMP.size: raise ValueError('truncated completions')
        for values in COMP.iter_unpack(raw):
            r=dict(zip(NAMES,values))
            for key in NAMES[3:14]:
                if r[key]: r[key]-=mapping['offset_ns']
            invalid_qpn=(not r['reserved']) if provider else bool(r['reserved'])
            if (r['worker']!=w['worker'] or invalid_qpn or r['bytes']!=65536 or r['offset']%65536
                    or not 0<=r['offset']<1048576 or not 0<r['enqueue']<=r['submit']<=r['poll_end']<=r['handled']
                    or not 0<=r['previous_begin']<=r['previous_end']<=r['poll_begin']<=r['poll_end']
                    or not 0<=r['drained_begin']<=r['drained_end']<=r['previous_end']
                    or not 0<r['loop_begin']<=r['phase_begin']<=r['poll_begin']):
                raise ValueError('invalid completion time/layout')
            if provider: r['hardware_qpn']=r['reserved']
            records[r['request']].append(r)
        raw=(root/w['gap_file']).read_bytes()
        if len(raw)!=w['gaps']*GAP.size: raise ValueError('truncated gaps')
        for values in GAP.iter_unpack(raw):
            g=dict(zip(GAP_NAMES,values)); key=(w['worker'],g['dev'])
            for name in GAP_NAMES[:5]:
                if g[name]: g[name]-=mapping['offset_ns']
            if not (0<g['previous_end']<=g['begin']<=g['end'] and g['end']-g['previous_end']>=1000000 and g['count']<=64):
                raise ValueError('invalid poll gap')
            if gaps[key] and g['previous_end']<gaps[key][-1]['end']: raise ValueError('overlapping poll gaps')
            gaps[key].append(g)
    return meta,records,gaps


def audit(root, single_rail=False):
    root=Path(root); meta,records,gaps=load(root); m=read_json(root/'manifest.json')
    requests=[json.loads(s) for s in (root/'requests.jsonl').read_text().splitlines()]
    if set(records)!={r['request_id'] for r in requests}: raise ValueError('request coverage differs')
    by_id={r['request_id']:r for r in requests}
    for epoch,slices in records.items():
        request=by_id[epoch]
        if request['status']!='success' or len(slices)!=16 or sorted(s['offset'] for s in slices)!=list(range(0,1048576,65536)):
            raise ValueError('missing/duplicate/failed Slice')
        if any(s['enqueue']<request['submitted_ns'] or s['poll_end']>request['finished_ns'] for s in slices):
            raise ValueError('request lifetime correlation differs')
    submitted=sorted(requests,key=lambda r:r['submitted_ns']); times=[r['submitted_ns'] for r in submitted]
    if single_rail:
        whitelist=read_json(root/'tent-config.json')['topology']['rdma_whitelist']
        names=m['environment']['nic_id_to_name']
        if len(whitelist)!=1 or any(names[str(s['dev'])]!=whitelist[0] for slices in records.values() for s in slices):
            raise ValueError('single Rail completion NIC differs from configuration')
        trace=[]
    else:
        _,trace=load_trace(root)
    for r in trace:
        q=submitted[bisect.bisect_right(times,r['ns'])-1]
        actual=defaultdict(int)
        for s in records[q['request_id']]: actual[s['dev']]+=s['bytes']
        if any(actual[r['dev'+str(j)]]!=r['assigned'+str(j)] for j in (0,1)):
            raise ValueError('completion NIC differs from allocation')
    result=dict(requests=len(requests),slices=sum(len(v) for v in records.values()),
        gaps=sum(len(v) for v in gaps.values()),workers=len(meta['workers']),exact_request_slice_nic_match=True,
        clock_mapping=meta['monotonic_mapping'])
    (root/'PD-audit.json').write_text(json.dumps(result,indent=2))
    return result


def overlap_index(gaps):
    return {key:([g['end'] for g in values],values) for key,values in gaps.items()}


def timing(s,index):
    start,end=s['submit'],s['poll_end']; duration=end-start
    ends,values=index.get((s['worker'],s['dev']),([],[]))
    n=bisect.bisect_right(ends,start); covered=0; maximum=0
    while n<len(values) and values[n]['previous_end']<end:
        g=values[n]; width=max(0,min(end,g['end'])-max(start,g['previous_end']))
        covered+=width;maximum=max(maximum,width);n+=1
    fraction=covered/duration if duration else 0.
    recent=(s['drained_begin']>=start and s['poll_begin']-s['drained_end']<=1000000
            and s['drained_end']-s['drained_begin']<=1000000 and s['poll_end']-s['poll_begin']<=1000000)
    category='dense_polling' if recent and fraction<=.1 else 'large_poll_service_gaps' if fraction>=.5 else 'mixed'
    return dict(post_cq_ms=duration/1e6,gap_fraction=fraction,max_overlap_ms=maximum/1e6,
        current_poll_ms=(s['poll_end']-s['poll_begin'])/1e6,
        last_not_full_before_ms=(end-s['drained_end'])/1e6 if s['drained_end']>=start else None,category=category)
