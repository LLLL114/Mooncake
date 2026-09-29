"""Replay the SQ controller independently and correlate every measured allocation."""
import bisect
import json
import math
from pathlib import Path
import struct
from oscillation_metrics import audit_trace as original_audit, load_trace, read_json

RECORD = struct.Struct('<4QdIi')


def audit_trace(root, policy, capacities):
    if policy == 0:
        return original_audit(root, False, False)[0]
    root = Path(root)
    meta = read_json(root / 'stable-quota.json')
    m, obs = read_json(root / 'manifest.json'), read_json(root / 'observer.json')
    if (meta['schema'] != 'tent-stable-quota-v1' or meta['record_bytes'] != 48 or meta['error']
            or meta['policy'] != policy or meta['capacity'] != capacities or obs['incomplete']
            or m['data_verified'] is not True):
        raise ValueError('invalid SQ metadata or correctness')
    raw = (root / 'stable-quota.bin').read_bytes()
    if len(raw) != meta['count'] * RECORD.size:
        raise ValueError('truncated SQ records')
    a, b = capacities
    mass = math.floor(a / (a + b) * 4096 + .5)
    if meta['base'] != mass / 4096:
        raise ValueError('incorrect target quantization')
    by_ns = {}
    last = window = samples = confirmations = offset = suggestion = 0
    total = 0.
    changes = 0
    for expected_sequence, (ns, q0, q1, sequence, weight, first, applied) in enumerate(RECORD.iter_unpack(raw)):
        if sequence != expected_sequence or ns <= last:
            raise ValueError('SQ sequence or timestamp mismatch')
        previous = offset
        if policy == 2:
            if not last or ns - last > 200000000:
                window = ns; total = 0.; samples = 0
                offset = suggestion = confirmations = 0
            total += q0 / a - q1 / b; samples += 1
            if ns - window >= 100000000:
                mean, h = total / samples, 1048576 / min(a, b)
                wanted = -1 if mean > h else 1 if mean < -h else 0
                confirmations = min(3, confirmations + 1) if wanted == suggestion else 1
                suggestion = wanted
                if confirmations == 3:
                    offset = wanted
                window = ns; total = 0.; samples = 0
        target_mass = min(4096, max(0, mass + offset * 256))
        # Rational cumulative allocation; no C++ table lookup here.
        phase = sequence % 256
        expected = ((phase + 1) * target_mass) // 256 - (phase * target_mass) // 256
        if applied != offset or first != expected or weight != target_mass / 4096:
            raise ValueError('controller or integer quota replay mismatch')
        changes += previous != offset
        by_ns[ns] = (q0, q1, weight, first)
        last = ns
    trace_meta, rows = load_trace(root)
    if trace_meta['fixed_equal'] or trace_meta['theoretical_override_gbps']:
        raise ValueError('unexpected old controls')
    req = sorted((json.loads(s) for s in (root / 'requests.jsonl').read_text().splitlines()), key=lambda r: r['submitted_ns'])
    if any(r['status'] != 'success' for r in req):
        raise ValueError('request failure')
    clock = [r['submitted_ns'] for r in req]
    start, end = m['measurement_start_ns'], m['measurement_end_ns']
    used, matched, probes, selectors = set(), set(), [], set()
    for i, r in enumerate(rows):
        if not start <= r['ns'] < end or r['site'] != 1 or r['mode'] not in (1, 2):
            raise ValueError('unexpected scope')
        if (r['total_bytes'], r['slices']) != (1048576, 16) or [r['dev0'], r['dev1']] != meta['rails']:
            raise ValueError('layout or mapping differs')
        selectors.add(r['selector'])
        for j in (0, 1):
            if m['environment']['nic_id_to_name'][str(r['dev'+str(j)])] != 'erdma_'+str(j):
                raise ValueError('wrong rail mapping')
            if r['penalty'+str(j)] != (1 if j == 0 else 10):
                raise ValueError('original diagnostic NUMA penalty changed')
        if r['mode'] == 1:
            q0, q1, weight, first = by_ns[r['ns']]
            if (q0, q1) != (r['inflight0'], r['inflight1']) or r['weight0'] != weight or r['weight1'] != 1-weight:
                raise ValueError('trace does not consume audited controller')
            counts = [first, 16-first]; matched.add(r['ns'])
        else:
            counts = [8, 8]; probes.append(i)
        if [r['assigned0'], r['assigned1']] != [x * 65536 for x in counts]:
            raise ValueError('actual quota differs from target table')
        k = bisect.bisect_right(clock, r['ns']) - 1
        if k < 0:
            raise ValueError('decision before submission')
        request = req[k]
        if request['phase'] != 'measurement' or request['finished_ns'] < r['ns'] or request['request_id'] in used:
            raise ValueError('request correlation mismatch')
        used.add(request['request_id'])
    if len(selectors) != 1 or not probes or any(b-a != 100 for a, b in zip(probes, probes[1:])):
        raise ValueError('selector or probe cadence differs')
    if matched != {ns for ns in by_ns if start <= ns < end}:
        raise ValueError('SQ to observer coverage gap')
    eligible = [r for r in req if r['phase'] == 'measurement' and start <= r['submitted_ns'] < end]
    missing = [r for r in eligible if r['request_id'] not in used]
    if len(missing) > 1 or (missing and missing[0]['submitted_ns'] != max(x['submitted_ns'] for x in eligible)):
        raise ValueError('internal request coverage gap')
    return dict(controller_records=meta['count'], decisions=len(rows), matched_normal=len(matched),
                probes=len(probes), whole_run_offset_changes=changes, base=meta['base'],
                independently_replayed=True, data_verified=m['data_verified'])
