#!/usr/bin/env python3
"""Explicit offline recovery of the unsupported-3s collection error; no rerun."""
import fcntl
import hashlib
import json
from pathlib import Path
import run_suite_collection_v2 as suite
from run_reference_rail_controls import ROOT,validate,hardware_counters,peer_check
from summarize_requests import summarize_requests


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    with (ROOT/'.suite.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        progress=suite._read(ROOT/'progress.json');cases=suite._read(ROOT/'plan.json')['cases']
        if progress['complete'] or len(progress['outcomes'])!=1 or len(cases)!=18:
            raise ValueError('only the first RC collection failure is eligible')
        item=progress['outcomes'][0];case=cases[0];root=Path(item['run_path'])
        if item['status']!='failed' or suite.case_hash(case)!=item['case_hash']:
            raise ValueError('failure/case identity differs')
        failure=suite._read(root/'failure.json');m=suite._read(root/'manifest.json');native=suite._read(root/'stream-summary.json')
        if failure!={'type':'ValueError','error':'latency_window_ms must be 250, 1000, or 5000','pending_unsafe':False}:
            raise ValueError('unexpected failure; no recovery')
        if m['native_return_code']!=0 or m['data_verified'] is not True or suite._read(root/'correctness.json')['passed'] is not True:
            raise ValueError('transport/data was not verified')
        if any(native['total'][key] for key in ('failure','pending','rejected')):
            raise ValueError('terminal native errors exist')
        raw_hashes={p.name:sha(p) for p in root.iterdir() if p.is_file()}
        records=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
        if len(records)!=native['total']['success'] or any(r['status']!='success' for r in records):
            raise ValueError('native/raw success conservation failed')
        summary,windows=summarize_requests(records,m,latency_window_ms=3000)
        summary['excluded_non_submitted_terminal_records']=0;summary['complete_run_accounting']=native
        suite._save(root/'summary.json',summary,exclusive=True);suite._save(root/'windows.json',windows,exclusive=True)
        audit=validate(case,dict(runPath=str(root),status='recovered_collection'))
        if any(sha(root/name)!=digest for name,digest in raw_hashes.items()): raise ValueError('original artifact changed')
        receipt=dict(reason='3s aggregation support added after transfer and receiver verification completed',
            original_attempt_status='failed',transport_rerun=False,original_files_sha256=raw_hashes,
            source={n:sha(Path(__file__).with_name(n)) for n in ('resume_reference_rail_controls.py','summarize_requests.py','verify_summarize_requests.py')},
            audit=audit,remaining_execution='continue original frozen plan cases[1:] with unchanged traffic parameters')
        suite._save(root/'collection-recovery.json',receipt,exclusive=True)
        item.update(status='recovered_collection',original_status='failed',recovery_receipt=str(root/'collection-recovery.json'),audit=audit)
        suite._save(ROOT/'progress.json',progress)
        print('RC_COLLECTION_RECOVERED_NO_RETRANSMISSION',len(records),flush=True)
        for c in cases[1:]:
            peer_check();counter=ROOT/'counters'/suite.case_hash(c)
            suite._save(counter/'before.json',hardware_counters(),exclusive=True)
            attempt=suite.run_case(c,ROOT)
            suite._save(counter/'after.json',hardware_counters(),exclusive=True)
            current=dict(case_hash=suite.case_hash(c),run_path=attempt.get('runPath'),status=attempt['status'])
            progress['outcomes'].append(current);suite._save(ROOT/'progress.json',progress)
            if attempt['status']!='success': raise ValueError('new failed run retained; no retry')
            try: current['audit']=validate(c,attempt)
            except Exception as error:
                current['audit_error']=str(error);suite._save(ROOT/'progress.json',progress);raise
            suite._save(ROOT/'progress.json',progress)
            print('RC_PROGRESS',len(progress['outcomes']),len(cases),flush=True)
        progress['complete']=True;suite._save(ROOT/'progress.json',progress)
    import analyze_reference_rail_controls
    analyze_reference_rail_controls.main()
    print('RC_RESUMED_COMPLETE',flush=True)


if __name__=='__main__':main()
