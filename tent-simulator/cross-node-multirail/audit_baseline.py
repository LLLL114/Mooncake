#!/usr/bin/env python3
"""SSH-only streaming audit of all saved attempts; does not rerun transfers."""
import collections
import json
from pathlib import Path
import sys

ROOT = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')


def read(path):
    return json.loads(path.read_text())


def audit(case, attempt):
    root = Path(attempt['runPath'])
    native = read(root / 'stream-summary.json')
    manifest = read(root / 'manifest.json')
    observer = read(root / 'observer.json')
    result = dict(run_path=str(root), attempt=attempt['attempt'], status=attempt['status'],
                  native_return_code=native['return_code'], stop_reason=native.get('stop_reason'),
                  data_verified=manifest.get('data_verified'), issues=[])
    errors = result['issues']
    if any(manifest.get(k) != v for k, v in native.items()):
        errors.append('native/manifest disagree')
    if native.get('drained') is not True or native.get('pending') != 0 or native.get('outstanding_batches') != 0:
        errors.append('not fully drained')
    if native.get('requests_output_complete') is not True or observer.get('incomplete') is not False:
        errors.append('incomplete request/observer output')
    ids, counts = set(), collections.defaultdict(collections.Counter)
    with (root / 'requests.jsonl').open() as stream:
        for line in stream:
            r = json.loads(line)
            identity, phase, status = r['request_id'], r['phase'], r['status']
            if type(identity) is not int or identity <= 0 or identity in ids:
                raise ValueError('invalid or duplicate request ID')
            ids.add(identity)
            if phase not in ('warmup', 'measurement') or status not in ('success', 'failure'):
                raise ValueError('unclassified request')
            if r['bytes'] != case['size']:
                raise ValueError('request bytes differ from case')
            if status == 'success' and (r.get('submitted_ns') is None or r.get('finished_ns') is None
                                        or r.get('transferred_bytes') != r['bytes']):
                raise ValueError('successful request lacks completed transfer evidence')
            for scope in ('total', phase):
                counts[scope]['accepted'] += 1
                counts[scope][status] += 1
    for scope, saved in [('top', native), *[(p, native[p]) for p in ('total', 'warmup', 'measurement')]]:
        actual = counts['total' if scope == 'top' else scope]
        if saved.get('pending') != 0 or any(saved.get(k) != actual[k] for k in ('accepted', 'success', 'failure')):
            errors.append(scope + ' terminal counts differ from records')
    result['records'] = len(ids)
    result['measurement'] = dict(counts['measurement'])
    correctness_path = root / 'correctness.json'
    if manifest.get('data_verified') is True:
        correct = read(correctness_path)
        checks = correct.get('checks', [])
        if correct.get('passed') is not True or not checks:
            errors.append('verified manifest lacks nonempty passing checks')
        epochs = native['final_epochs']
        expected = {i: e for i, e in enumerate(epochs) if e is not None}
        checked = {}
        for c in checks:
            if (c['slot'] in checked or c.get('passed') is not True or c.get('guard_ok') is not True
                    or not isinstance(c.get('sha256'), str) or len(c['sha256']) != 64
                    or c.get('sha256') != c.get('expected_sha256') or c.get('bytes') != case['size']):
                errors.append('invalid final payload/guard check')
            checked[c['slot']] = c['epoch']
        if checked != expected:
            errors.append('checks/final epochs disagree')
        result['checked_slots'] = len(checks)
    else:
        result['checked_slots'] = 0
        result['verification_scope'] = 'payload not verified in this attempt; preserve original failure'
        if attempt['status'] == 'success':
            errors.append('successful attempt lacks data verification')
    return result


def main():
    output = ROOT / 'reports/baseline-20260916/all-attempts-audit.json'
    if output.exists():
        raise ValueError('audit already exists; do not overwrite')
    results = []
    for state_path in sorted((ROOT / 'suites-extended/baseline').glob('*/state.json')):
        state = read(state_path)
        for attempt in state['attempts']:
            try:
                result = audit(state['case'], attempt)
            except (OSError, ValueError, KeyError, TypeError) as error:
                result = dict(run_path=attempt.get('runPath'), attempt=attempt['attempt'],
                              status=attempt['status'], issues=[str(error)])
            result.update(case_hash=state['caseHash'], latest=attempt is state['attempts'][-1])
            results.append(result)
        if len(results) % 10 == 0:
            print('AUDITED', len(results), flush=True)
    report = dict(scope='saved evidence audit; not a new transfer or fresh receiver verification',
                  attempts=len(results), latest_attempts=sum(r['latest'] for r in results),
                  record_count=sum(r.get('records', 0) for r in results),
                  issue_attempts=sum(bool(r['issues']) for r in results),
                  unverified_attempts=[r['run_path'] for r in results if r.get('data_verified') is not True],
                  results=results)
    with output.open('x') as stream:
        json.dump(report, stream, indent=2)
    print('AUDIT_COMPLETE', report['attempts'], 'ISSUES', report['issue_attempts'], flush=True)
    return 1 if report['issue_attempts'] else 0


if __name__ == '__main__':
    sys.exit(main())
