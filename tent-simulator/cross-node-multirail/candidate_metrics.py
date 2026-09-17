"""Independent reconstruction of captured candidate state and real allocation."""
import bisect
from collections import defaultdict
import math
from pathlib import Path
import struct
import json
from oscillation_metrics import FIELDS, read_json

FORMAT = struct.Struct('<4Q4I2i4Q11d2Q')
NAMES = FIELDS + ['previous_filtered', 'previous_applied', 'filtered', 'previous_ns', 'decision_ns']
assert FORMAT.size == 192


def load_trace(root):
    root = Path(root)
    meta = read_json(root / 'oscillation-trace.json')
    if (meta['schema'] != 'tent-candidate-trace-v1' or meta['record_bytes'] != 192 or meta['error']
            or meta['fixed_equal'] or meta['theoretical_override_gbps'] != 0):
        raise ValueError('invalid candidate metadata')
    rows = []
    for slot in meta['slots']:
        if slot['dropped'] or Path(slot['file']).name != slot['file']:
            raise ValueError('overflow or invalid path')
        raw = (root / slot['file']).read_bytes()
        if len(raw) != slot['count'] * FORMAT.size:
            raise ValueError('truncated candidate records')
        for i, fields in enumerate(FORMAT.iter_unpack(raw)):
            r = dict(zip(NAMES, fields)); r['owner'] = slot['owner']
            if r['sequence'] != i:
                raise ValueError('sequence gap')
            rows.append(r)
    if not rows or len({r['owner'] for r in rows}) != 1:
        raise ValueError('requires one producer')
    rows.sort(key=lambda r: r['ns'])
    if any(a['ns'] >= b['ns'] for a, b in zip(rows, rows[1:])):
        raise ValueError('nonmonotonic decisions')
    return meta, rows


def expected_weight(r, kind):
    inv = [1 / (r['score' + str(j)] + 1e-12) for j in (0, 1)]
    raw = inv[0] / sum(inv)
    if r['mode'] == 2 or kind == 0:
        return raw, raw
    if r['decision_ns'] != r['ns']:
        raise ValueError('filter and observer clock differ')
    dt = r['decision_ns'] - r['previous_ns']
    if not r['previous_ns'] or dt >= 100000000 or kind == 3:
        filtered = applied = raw
    else:
        if dt <= 0:
            raise ValueError('invalid filter interval')
        gain = dt / (10000000. + dt)
        filtered = r['previous_filtered'] + gain * (raw - r['previous_filtered'])
        applied = filtered if kind == 1 or abs(filtered - r['previous_applied']) >= .02 else r['previous_applied']
    if abs(filtered - r['filtered']) > 1e-12:
        raise ValueError('filter state does not follow specified recurrence')
    return raw, applied


def expected_quota(w, kind, probe=False, w1=None):
    if probe:
        return [8, 8]
    exact = [w * 16, (1 - w if w1 is None else w1) * 16]
    counts = [int(x) for x in exact]
    best = (0 if exact[0] - counts[0] >= exact[1] - counts[1] else 1) if kind == 3 else (0 if w >= .5 else 1)
    counts[best] += 16 - sum(counts)
    return counts


def audit_trace(root, kind):
    root = Path(root)
    meta, rows = load_trace(root)
    m, observer = read_json(root / 'manifest.json'), read_json(root / 'observer.json')
    if meta['candidate_policy'] != kind or observer['incomplete'] or m['data_verified'] is not True:
        raise ValueError('invalid trace policy, completeness or correctness')
    start, end = m['measurement_start_ns'], m['measurement_end_ns']
    req = sorted([json.loads(s) for s in (root / 'requests.jsonl').read_text().splitlines()], key=lambda r: r['submitted_ns'])
    if any(r['status'] != 'success' for r in req):
        raise ValueError('failed request')
    clock = [r['submitted_ns'] for r in req]
    weights = defaultdict(list); used = set(); selectors = set(); contexts = set(); probes = []
    previous = None
    for i, r in enumerate(rows):
        if not start <= r['ns'] < end or r['site'] != 1 or r['mode'] not in (1, 2):
            raise ValueError('unexpected trace scope')
        if (r['total_bytes'], r['slices']) != (1048576, 16) or r['dev0'] >= r['dev1']:
            raise ValueError('unexpected request layout')
        selectors.add(r['selector']); contexts.add((r['context'], r['mode']))
        if r['mode'] == 2:
            probes.append(i)
        raw, expected = expected_weight(r, kind)
        r['raw_weight0'] = raw
        w = [r['weight0'], r['weight1']]
        if any(not math.isfinite(x) or x < 0 or x > 1 for x in w) or abs(sum(w) - 1) > 1e-12:
            raise ValueError('invalid normalized weights')
        if abs(w[0] - expected) > 1e-12:
            raise ValueError('applied weight differs from independently reconstructed policy')
        if r['mode'] == 1 and kind:
            if previous is not None and r['previous_ns']:
                if (r['previous_ns'] != previous['decision_ns'] or
                    abs(r['previous_filtered'] - previous['filtered']) > 1e-12 or
                    abs(r['previous_applied'] - previous['weight0']) > 1e-12):
                    raise ValueError('filter continuity gap')
            elif previous is not None and r['ns'] - previous['ns'] < 100000000:
                raise ValueError('unexplained filter reset')
            previous = r
        quota = expected_quota(w[0], kind, r['mode'] == 2, w1=w[1])
        if [r['assigned0'], r['assigned1']] != [n * 65536 for n in quota]:
            raise ValueError('actual slice bytes differ from policy')
        k = bisect.bisect_right(clock, r['ns']) - 1
        if k < 0:
            raise ValueError('decision precedes request')
        q = req[k]
        if q['phase'] != 'measurement' or q['finished_ns'] < r['ns'] or q['request_id'] in used:
            raise ValueError('request correlation failed')
        used.add(q['request_id'])
        for j in (0, 1):
            name = m['environment']['nic_id_to_name'][str(r['dev' + str(j)])]
            if r['penalty' + str(j)] != (1 if name == 'erdma_0' else 10):
                raise ValueError('unexpected NUMA penalty')
            weights[(r['selector'], r['context'], (r['ns'] - start) // 50000000, r['dev' + str(j)])].append(w[j])
    if len(selectors) != 1 or len(contexts) != 2 or {c[1] for c in contexts} != {1, 2}:
        raise ValueError('mixed contexts or missing probe')
    if any(b - a != 100 for a, b in zip(probes, probes[1:])):
        raise ValueError('probe cadence changed')
    eligible = [r for r in req if r['phase'] == 'measurement' and start <= r['submitted_ns'] < end]
    missing = [r for r in eligible if r['request_id'] not in used]
    if len(missing) > 1 or (missing and missing[0]['submitted_ns'] != max(r['submitted_ns'] for r in eligible)):
        raise ValueError('internal trace coverage gap')
    seen = set()
    for t in observer['threads']:
        definitions = {c['id']: c for c in t['contexts']}
        for row in t['rows']:
            if row['kind'] != 'weight':
                continue
            c = definitions[row['context']]
            key = (c['selector'], c['id'], row['bin'], row['dev'])
            if key not in weights:
                continue
            values = weights[key]
            if key in seen or row['n'] != len(values):
                raise ValueError('observer count mismatch')
            v = row['values'][0]
            if abs(v['sum'] - sum(values)) > 1e-8 * len(values) or abs(v['last'] - values[-1]) > 1e-12:
                raise ValueError('observer weight mismatch')
            semantics = ('consumed_filtered_weight' if kind in (1, 2) else 'consumed_inverse_score') if c['mode'] == 1 else 'diagnostic_inverse_score_only'
            if c['weight_semantics'] != semantics:
                raise ValueError('weight semantics mislabeled')
            seen.add(key)
    if seen != set(weights):
        raise ValueError('missing 50ms crosscheck')
    return dict(passed=True, records=len(rows), probes=len(probes), boundary_missing=len(missing),
                raw_and_applied_weight_checked=True, state_continuity_checked=True,
                actual_slice_bytes_checked=True, observer_crosschecked=True), rows
