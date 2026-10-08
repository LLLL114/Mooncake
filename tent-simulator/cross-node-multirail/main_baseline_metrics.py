"""Validate current-main runs and exact request-linked decision records."""
import hashlib
import json
import math
from pathlib import Path
from oscillation_metrics import FORMAT,FIELDS,read_json


def load_trace(root):
    root=Path(root);meta=read_json(root/'observer.json')
    if (meta['schema']!='tent-main-selection-v1' or meta['error'] or meta['incomplete']
            or meta['dropped'] or meta['record_bytes']!=152 or meta['clock']!='CLOCK_MONOTONIC'):
        raise ValueError('invalid decision observation')
    raw=(root/'main-selection.bin').read_bytes()
    if len(raw)!=meta['count']*FORMAT.size or not meta['count']:raise ValueError('empty/truncated trace')
    rows=[dict(zip(FIELDS,v)) for v in FORMAT.iter_unpack(raw)]
    return meta,rows


def validate(case,root):
    root=Path(root);p=case['parameters'];m=read_json(root/'manifest.json');native=read_json(root/'stream-summary.json')
    if m['native_return_code']!=0 or m['data_verified'] is not True or read_json(root/'correctness.json')['passed'] is not True:
        raise ValueError('transport or payload verification failed')
    total=native['total']
    if any(total[k] for k in ('failure','pending','rejected')) or total['accepted']!=total['success'] or total['submitted']!=total['success']:
        raise ValueError('request conservation failed')
    if native['start_missed'] or native['failure_reason'] is not None or native['outstanding_slots']:
        raise ValueError('native timeline/terminal state invalid')
    arguments=read_json(root/'arguments.json')
    if any(arguments.get(k)!=v for k,v in p.items()):raise ValueError('arguments differ from frozen plan')
    if m['receiver'].get('source_commit')!=case['context']['main'] or m['receiver'].get('rdma_num_lanes')!=6:
        raise ValueError('receiver source/lane mismatch')
    for name,key in (('tent_library','tent_sha256'),('stream_library','stream_sha256')):
        if m['environment'][key]!=case['context'][name+'_sha256'] or hashlib.sha256(Path(p[name]).read_bytes()).hexdigest()!=m['environment'][key]:
            raise ValueError('loaded library provenance differs')
    config=read_json(root/'tent-config.json')
    if config['topology']['rdma_whitelist']!=['erdma_0','erdma_1'] or not config['transports']['rdma']['enable_smart_scheduling']:
        raise ValueError('original dual-Rail scheduling not enabled')
    if any(config['transports']['rdma'].get(k) is not None for k in ('bandwidth_learning_rate','score_jitter_range','default_bandwidth_gbps')):
        raise ValueError('main scheduling defaults were overridden')
    requests=[json.loads(s) for s in (root/'requests.jsonl').read_text().splitlines()]
    by_id={q['request_id']:q for q in requests}
    if len(requests)!=len(by_id) or len(requests)!=total['success'] or any(q['status']!='success' or q['bytes']!=1048576 for q in requests):
        raise ValueError('raw request coverage differs')
    start,end=m['measurement_start_ns'],m['measurement_end_ns']
    measured=[q for q in requests if q['phase']=='measurement']
    if end-start!=round(p['seconds']*1e9) or not measured or any(not start<=q['planned_ns']<end for q in measured):
        raise ValueError('measurement arrival cohort invalid')
    if p['rate'] and len(measured)!=round(p['seconds']*p['rate']/p['size']):
        raise ValueError('offered request count differs')
    result=dict(success_requests=len(requests),measurement_requests=len(measured),traced=case['traced'],payload_verified=True)
    if case['traced']:
        meta,rows=load_trace(root);used=set();probes=[]
        if (meta['start_ns'],meta['end_ns'])!=(start,end) or len({r['selector'] for r in rows})!=1:
            raise ValueError('trace epoch/selector differs')
        for i,r in enumerate(rows):
            epoch=r['context'];q=by_id.get(epoch)
            if (r['sequence']!=i or r['site']!=1 or epoch in used or q is None or r['slices']!=16
                    or r['total_bytes']!=1048576 or r['mode'] not in (1,2) or r['dev0']>=r['dev1']
                    or not start<=r['ns']<end or not q['submitted_ns']<=r['ns']<=q['finished_ns']):
                raise ValueError('decision/request identity invalid')
            if i and rows[i-1]['ns']>=r['ns']:raise ValueError('decision timestamps not increasing')
            used.add(epoch)
            names=m['environment']['nic_id_to_name']
            if {names[str(r['dev0'])],names[str(r['dev1'])]}!={'erdma_0','erdma_1'}:
                raise ValueError('trace device IDs differ from actual NIC topology')
            score=[r['score0'],r['score1']];weight=[r['weight0'],r['weight1']]
            inv=[1/(s+meta['epsilon']) for s in score]
            if any(not math.isfinite(w) or abs(w-inv[j]/sum(inv))>1e-12 for j,w in enumerate(weight)):
                raise ValueError('captured weight does not match consumed inverse score')
            expected=[int(w*16) for w in weight]
            if r['mode']==2:expected=[8,8];probes.append(i)
            else:expected[0 if inv[0]>=inv[1] else 1]+=16-sum(expected)
            if [r['assigned0'],r['assigned1']]!=[n*65536 for n in expected]:
                raise ValueError('actual assignment differs from main algorithm')
        if any(b-a!=100 for a,b in zip(probes,probes[1:])):raise ValueError('probe cadence differs')
        expected={q['request_id'] for q in requests if start<=q['submitted_ns']<end}
        missing=expected-used
        if len(missing)>1 or any(end-by_id[i]['submitted_ns']>1000000 for i in missing):
            raise ValueError('missing request allocation records')
        column=0 if m['environment']['nic_id_to_name'][str(rows[0]['dev0'])]=='erdma_0' else 1
        result.update(decisions=len(rows),normal=sum(r['mode']==1 for r in rows),probes=len(probes),boundary_missing=len(missing),exact_weights_and_assignment=True,rail0_column=column)
    return result
