#!/usr/bin/env python3
"""SSH-only phase/event analysis; no cross-phase CV interpreted as oscillation."""
import bisect
import json
from pathlib import Path
import sys

from analyze_observer import analyze, spread
from summarize_requests import nearest_rank
from run_followups_bc import validate

BASE = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')


def quantile_ms(values, minimum=1):
    return nearest_rank(values, 990) / 1e6 if len(values) >= minimum else None


def cohort(records, start, end):
    return [r for r in records if start <= r['planned_ns'] < end]


def backlog(records, when):
    return sum(r['planned_ns'] <= when < r['finished_ns'] for r in records)


def sustained(windows, predicate, required=5):
    for i in range(len(windows) - required + 1):
        block = windows[i:i + required]
        if all(predicate(w) for w in block) and all(a['right'] == b['left'] for a, b in zip(block, block[1:])):
            return dict(first_window_start_s=block[0]['left'], confirmed_at_s=block[-1]['right'])
    return dict(first_window_start_s=None, confirmed_at_s=None, right_censored=True)


def phase_summary(records, series, origin, left, right, offered, name, latency_windows):
    start, end = origin + round(left * 1e9), origin + round(right * 1e9)
    selected = cohort(records, start, end)
    completed = [r for r in records if start <= r['finished_ns'] < end]
    windows = [w for w in series if left <= w['left'] and w['right'] <= right]
    p99_windows = [w['p99_end_to_end_ns']/1e6 for w in latency_windows
                   if start<=w['start_ns'] and w['end_ns']<=end and w['duration_ns']==1_000_000_000
                   and w['success_requests']>=500 and w['p99_end_to_end_ns'] is not None]
    return dict(name=name, start_s=left, end_s=right, offered_gbps=offered,
                arrivals=len(selected), completed=len(completed),
                goodput_gbps=sum(r['bytes'] for r in completed) * 8e-9 / (right-left),
                p99_ms=quantile_ms([r['finished_ns']-r['planned_ns'] for r in selected], 500),
                queue_p99_ms=quantile_ms([r['submitted_ns']-r['planned_ns'] for r in selected], 500),
                service_p99_ms=quantile_ms([r['finished_ns']-r['submitted_ns'] for r in selected], 500),
                p99_window_variability_ms=spread(p99_windows,20),
                throughput_250ms=spread([w['gbps'] for w in windows]),
                allocation_share_erdma0=spread([w['share0'] for w in windows if w['share0'] is not None]),
                jain_250ms=spread([w['jain'] for w in windows if type(w['jain']) in (int,float)]),
                backlog_at_start=backlog(records,start), backlog_at_end=backlog(records,end))


def run(case, attempt, capacity):
    validate(case, attempt)
    root = Path(attempt['runPath'])
    read = lambda n: json.loads((root / (n+'.json')).read_text())
    manifest, summary, windows = read('manifest'), read('summary'), read('windows')
    records = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
    origin, finish = manifest['measurement_start_ns'], manifest['measurement_end_ns']
    data = analyze(read('observer'), manifest, capacity, summary, windows)
    if data['incomplete'] or 'observer_metrics' in data['SUMMARY']:
        raise ValueError('incomplete or misaligned observer')
    mapping = manifest['environment']['nic_id_to_name']
    device = next(k for k,v in mapping.items() if v=='erdma_0')
    series = []
    for w in data['windows']['250ms']['windows']:
        shares = w['shares']['allocation']
        series.append(dict(left=(w['start_ns']-origin)/1e9,right=(w['end_ns']-origin)/1e9,
                           gbps=w['request_completion']['bits_per_second']/1e9,
                           share0=shares.get(device,0.0) if shares is not None else None,
                           jain=w['nic_utilization'].get('jain')))
    planned = sorted(r['planned_ns'] for r in records)
    completed = sorted(r['finished_ns'] for r in records)
    curve = [dict(seconds=(t-origin)/1e9, pending=bisect.bisect_right(planned,t)-bisect.bisect_right(completed,t))
             for t in range(origin,finish+1,50_000_000)]
    rate = case['capacity_reference']['d0_bytes_per_second']*8e-9
    profile = case['load']
    bounds = [(0,20,.2,'initial_low'),(20,40,.8,'high'),(40,60,.2,'final_low')] if profile=='step' else [
             (0,20,.2,'initial_low'),(20,60,.25,'pulse_train'),(60,80,.2,'final_low')]
    phases = [phase_summary(records,series,origin,l,r,f*rate,n,windows['latency']['windows']) for l,r,f,n in bounds]
    result = dict(run_path=str(root), profile=profile, repeat=case['repeat'],
                  counts=manifest['total'], measured_counts=manifest['measurement'], data_verified=manifest['data_verified'],
                  observer_complete=True, phases=phases, series_250ms=series, backlog_50ms=curve,
                  retry_events=sum(data['SUMMARY']['io_diagnostics'][k]['events'] for k in ('retry_endpoint','retry_post','retry_cq')))
    if profile=='step':
        transitions = []
        for left,right,fraction,name in bounds[1:]:
            subset = [w for w in series if left<=w['left'] and w['right']<=right]
            target = fraction*rate
            reference = [w['share0'] for w in subset if w['left']>=right-10 and w['share0'] is not None]
            stats = spread(reference)
            stable = len(reference)==40 and stats['sd']<=.05
            throughput = sustained(subset,lambda w: abs(w['gbps']/target-1)<=.1)
            joint = sustained(subset,lambda w: w['share0'] is not None and abs(w['share0']-stats['mean'])<=.05
                              and abs(w['gbps']/target-1)<=.1) if stable else dict(reason='reference share SD >5pp or incomplete')
            for estimate in (throughput,joint):
                for key in ('first_window_start_s','confirmed_at_s'):
                    if estimate.get(key) is not None:estimate[key]-=left
            transitions.append(dict(at_s=left,to=name,reference_share=stats,reference_stable=stable,
                                    throughput_settling=throughput,joint_settling=joint,
                                    observed_seconds=right-left))
        result['transitions']=transitions
    else:
        reference = [w['pending'] for w in curve if 10<=w['seconds']<20]
        threshold = nearest_rank(reference,950)+1
        events=[]
        pulse_records=[]
        for i in range(20):
            start,end,next_start=20+2*i,20+2*i+.1,22+2*i
            selected=cohort(records,origin+round(start*1e9),origin+round(end*1e9))
            pulse_records.extend(selected)
            samples=[w for w in curve if end<=w['seconds']<next_start]
            recovery=None
            for j in range(len(samples)-5):
                group=samples[j:j+6]  # Six 50ms grid points span 250ms.
                if all(w['pending']<=threshold for w in group):
                    recovery=group[-1]['seconds']-end;break
            events.append(dict(at_s=start,arrivals=len(selected),backlog_peak=max(
                w['pending'] for w in curve if start<=w['seconds']<next_start),
                recovery_confirmed_s=recovery,censored_after_s=next_start-end if recovery is None else None,
                pulse_p99_ms=quantile_ms([r['finished_ns']-r['planned_ns'] for r in selected],500)))
        result.update(pulse_events=events,backlog_reference_p95_plus1=threshold,
                      pooled_pulse_cohort_p99_ms=quantile_ms([r['finished_ns']-r['planned_ns'] for r in pulse_records],500))
    return result


def main():
    if '--self-test' in sys.argv:
        records=[dict(planned_ns=1,submitted_ns=2,finished_ns=11,bytes=1)]
        assert len(cohort(records,0,10))==1 and backlog(records,10)==1 and backlog(records,11)==0
        assert quantile_ms([1]*499,500) is None and quantile_ms([1000000]*500,500)==1
        assert sustained([dict(left=i,right=i+1,ok=i!=2) for i in range(6)],lambda w:w['ok'])['right_censored']
        print('B_ANALYSIS_BOUNDARIES_PASS');return
    suite=BASE/'followups-bc';progress=json.loads((suite/'progress-B.json').read_text())
    if progress['complete'] is not True or len(progress['outcomes'])!=10:raise ValueError('B incomplete')
    output=BASE/'reports/B-20260916';output.mkdir(parents=True,exist_ok=True)
    if (output/'summary.json').exists():raise ValueError('refusing to overwrite analysis')
    capacity=json.loads((BASE/'suites-extended/capacity.json').read_text());results=[]
    for item in progress['outcomes']:
        state=json.loads((suite/'B'/item['case_hash']/'state.json').read_text())
        result=run(state['case'],state['attempts'][-1],capacity);result['case_hash']=item['case_hash'];results.append(result)
    report=dict(scope='B only; offline diagnostic thresholds, not an online convergence detector',
                definitions=dict(throughput='completion-time windows',latency='planned-arrival cohorts including later completions',
                  backlog='planned but not yet completed at each 50ms sample',
                  settling='five consecutive 250ms windows within offered throughput +/-10%; joint also within reference share +/-5pp',
                  share_reference='last 10s of destination phase, available only with SD<=5pp and 40 full windows',
                  pulse_recovery='backlog <=pre-train last10s P95+1 for six 50ms samples; confirmed at sixth sample',
                  quantiles='nearest rank; >=500 successful arrivals; no per-pulse P99 when sparse',
                  reversals='native phase-specific weight reversals unavailable; no cross-phase CV interpreted as oscillation'),runs=results)
    with (output/'summary.json').open('x') as f:json.dump(report,f,indent=2)
    print('B_ANALYSIS_COMPLETE',len(results),str(output),flush=True)


if __name__=='__main__':main()
