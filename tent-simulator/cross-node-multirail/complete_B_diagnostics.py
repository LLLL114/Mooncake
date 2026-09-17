#!/usr/bin/env python3
"""SSH-only: distinguish a short in-band interval from persistent stability."""
import json
from pathlib import Path

ROOT = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail/reports/B-20260916')


def main():
    source = json.loads((ROOT/'summary.json').read_text())
    results = []
    for run in source['runs']:
        result = dict(run_path=run['run_path'],profile=run['profile'],repeat=run['repeat'])
        if run['profile']=='step':
            result['after_confirmation']=[]
            for transition in run['transitions']:
                phase=next(p for p in run['phases'] if p['name']==transition['to'])
                confirmation=transition['throughput_settling'].get('confirmed_at_s')
                row=dict(to=transition['to'],first_confirmation_s=confirmation)
                if confirmation is not None:
                    samples=[w for w in run['series_250ms'] if transition['at_s']+confirmation<=w['left']
                             and w['right']<=phase['end_s']]
                    flags=[abs(w['gbps']/phase['offered_gbps']-1)>.1 for w in samples]
                    stretches=[];length=0
                    for flag in flags:
                        if flag:length+=1
                        elif length:stretches.append(length);length=0
                    if length:stretches.append(length)
                    row.update(windows=len(flags),outside_windows=sum(flags),
                               outside_fraction=sum(flags)/len(flags) if flags else None,
                               outside_excursions=len(stretches),longest_outside_s=max(stretches,default=0)*.25)
                result['after_confirmation'].append(row)
        else:
            root=Path(run['run_path']);m=json.loads((root/'manifest.json').read_text())
            records=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            result['pulse_completion']=[]
            for event in run['pulse_events']:
                start=m['measurement_start_ns']+round(event['at_s']*1e9)
                selected=[r for r in records if start<=r['planned_ns']<start+100_000_000]
                if not selected or any(r['status']!='success' for r in selected):
                    raise ValueError('pulse lacks successful request cohort')
                last=max(r['finished_ns'] for r in selected)
                result['pulse_completion'].append(dict(at_s=event['at_s'],arrivals=len(selected),
                    last_completion_after_pulse_end_s=(last-start-100_000_000)/1e9,
                    all_finished_before_next_pulse=last<start+2_000_000_000,
                    backlog_band_recovery_censored=event['recovery_confirmed_s'] is None))
        results.append(result)
    report=dict(scope='diagnostics supplementary to frozen B analysis; no changed thresholds',
                interpretation='five in-band windows do not imply permanent stability; backlog-band recovery and pulse-cohort completion differ',
                runs=results)
    with (ROOT/'supplement.json').open('x') as f:json.dump(report,f,indent=2)
    print('B_SUPPLEMENT_COMPLETE',len(results))


if __name__=='__main__':main()
