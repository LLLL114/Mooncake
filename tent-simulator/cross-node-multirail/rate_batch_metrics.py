"""Independent 152B decision / 96B estimator validation, no transport changes."""
import bisect
from collections import defaultdict
import json
import math
from pathlib import Path
import struct
from oscillation_metrics import FORMAT, FIELDS, read_json

RATE_FORMAT = struct.Struct('<9Q3d')
RATE_NAMES = ('ns dev done previous_done interval send_span minimum_pending valid reason '
              'previous_capacity observed_rate capacity').split()


def close(a, b):
    return math.isfinite(a) and math.isfinite(b) and abs(a - b) <= 1e-11 * max(1., abs(a), abs(b))


def load_trace(root):
    root = Path(root); meta = read_json(root / 'oscillation-trace.json')
    if meta['schema'] == 'tent-oscillation-trace-v1':
        from oscillation_metrics import load_trace as original_load
        return original_load(root)
    if (meta['schema'] != 'tent-rate-batch-trace-v1' or meta['record_bytes'] != 152 or meta['error']
            or meta['fixed_equal'] or meta['theoretical_override_gbps'] != 0):
        raise ValueError('invalid RB decision metadata')
    rows = []
    for slot in meta['slots']:
        if slot['dropped'] or Path(slot['file']).name != slot['file']:
            raise ValueError('overflow or invalid path')
        raw = (root / slot['file']).read_bytes()
        if len(raw) != slot['count'] * FORMAT.size:
            raise ValueError('truncated decision records')
        for i, fields in enumerate(FORMAT.iter_unpack(raw)):
            r = dict(zip(FIELDS, fields)); r['owner'] = slot['owner']
            if r['sequence'] != i:
                raise ValueError('trace sequence gap')
            rows.append(r)
    if not rows or len({r['owner'] for r in rows}) != 1:
        raise ValueError('requires one decision producer')
    rows.sort(key=lambda r: r['ns'])
    if any(a['ns'] >= b['ns'] for a, b in zip(rows, rows[1:])):
        raise ValueError('nonmonotonic decisions')
    return meta, rows


def batch_expected(q0, q1, c0, c1, n=16, size=65536):
    target_bytes = max(0., min(n * size, (c0 * n * size + c0 * q1 - c1 * q0) / (c0 + c1)))
    target = target_bytes / size
    # Independent exhaustive integer solution, not the C++ four-point implementation.
    def objective(k):
        a = (q0 + k * size) / c0 if k else 0.
        b = (q1 + (n - k) * size) / c1 if k < n else 0.
        return max(a, b), abs(k - target), k
    best = min(range(n + 1), key=objective)
    return target / n, [best, n - best]


def rates(root, policy, seeds, manifest):
    root = Path(root); meta = read_json(root / 'rate-estimates.json')
    if meta['schema'] != 'tent-rb-rate-v1' or meta['record_bytes'] != 96 or meta['error'] or meta['policy'] != policy:
        raise ValueError('invalid rate metadata')
    if meta['seed_bps'] != seeds or len(meta['rails']) != 2:
        raise ValueError('seed or rail count mismatch')
    expected_bytes = manifest['total']['accepted'] * 1048576
    if (any(x['pending'] or x['invalid_completions'] or x['done'] != x['posted'] for x in meta['rails'])
            or sum(x['done'] for x in meta['rails']) != expected_bytes):
        raise ValueError('whole-run post/completion accounting mismatch')
    dev_seed = {}
    for x in meta['rails']:
        name = manifest['environment']['nic_id_to_name'][str(x['dev'])]
        if name != 'erdma_' + str(x['rail']):
            raise ValueError('rate rail mapping mismatch')
        dev_seed[x['dev']] = seeds[x['rail']]
    raw = (root / 'rate-estimates.bin').read_bytes()
    if len(raw) != meta['count'] * RATE_FORMAT.size:
        raise ValueError('truncated estimator events')
    events = [dict(zip(RATE_NAMES, x)) for x in RATE_FORMAT.iter_unpack(raw)]
    by_dev = defaultdict(list)
    if policy not in (3, 5) and events:
        raise ValueError('unexpected learning in fixed/legacy policy')
    for e in events:
        d = e['dev']; seed = dev_seed[d]; previous = by_dev[d][-1] if by_dev[d] else None
        if previous is None:
            if e['reason'] != 1 or e['valid'] or e['previous_done'] or not close(e['capacity'], seed):
                raise ValueError('invalid estimator initialization')
        else:
            if e['interval'] != e['ns'] - previous['ns'] or e['previous_done'] != previous['done']:
                raise ValueError('rate interval/counter gap')
            if not close(e['previous_capacity'], previous['capacity']):
                raise ValueError('rate state continuity gap')
            delta = e['done'] - previous['done']
            if delta < 0:
                raise ValueError('completed counter regressed')
            reason = (2 if e['interval'] < 5000000 else 0) | (4 if delta < 262144 else 0)
            reason |= 8 if e['minimum_pending'] < 131072 or e['minimum_pending'] == 2**64 - 1 else 0
            if e['reason'] != reason or bool(e['valid']) != (reason == 0):
                raise ValueError('rate validity gate mismatch')
            observed = delta * 1e9 / max(e['interval'], e['send_span'])
            if not close(e['observed_rate'], observed):
                raise ValueError('rate sample differs from counters')
            wanted = previous['capacity']
            if not reason:
                bounded = min(seed * 2, max(seed * .05, observed))
                wanted += e['interval'] / (20000000. + e['interval']) * (bounded - wanted)
            if not close(e['capacity'], wanted):
                raise ValueError('rate learning recurrence mismatch')
        by_dev[d].append(e)
    if policy in (3, 5) and set(by_dev) != set(dev_seed):
        raise ValueError('missing adaptive rate observations')
    return meta, by_dev, dev_seed


def audit_trace(root, policy, seeds):
    root = Path(root); meta, rows = load_trace(root)
    m, observer = read_json(root / 'manifest.json'), read_json(root / 'observer.json')
    if meta['rate_batch_policy'] != policy or observer['incomplete'] or m['data_verified'] is not True:
        raise ValueError('invalid policy, observation or correctness')
    rate_meta, rate_events, dev_seed = rates(root, policy, seeds, m)
    rate_times = {d: [e['ns'] for e in events] for d, events in rate_events.items()}
    start, end = m['measurement_start_ns'], m['measurement_end_ns']
    req = sorted([json.loads(s) for s in (root / 'requests.jsonl').read_text().splitlines()], key=lambda r: r['submitted_ns'])
    if any(r['status'] != 'success' for r in req):
        raise ValueError('failed request')
    clock = [r['submitted_ns'] for r in req]
    weights = defaultdict(list); used = set(); selectors = set(); contexts = set(); probes = []
    for i, r in enumerate(rows):
        if not start <= r['ns'] < end or r['site'] != 1 or r['mode'] not in (1, 2):
            raise ValueError('unexpected decision scope')
        if (r['total_bytes'], r['slices']) != (1048576, 16) or r['dev0'] >= r['dev1']:
            raise ValueError('unexpected request layout')
        selectors.add(r['selector']); contexts.add((r['context'], r['mode']))
        if r['mode'] == 2:
            probes.append(i)
        inv = [1 / (r['score' + str(j)] + 1e-12) for j in (0, 1)]
        expected = inv[0] / sum(inv)
        w = [r['weight0'], r['weight1']]
        if any(not math.isfinite(x) or x < 0 or x > 1 for x in w) or abs(sum(w) - 1) > 1e-12:
            raise ValueError('invalid normalized weights')
        if r['mode'] == 2:
            quota = [8, 8]
        elif policy >= 4:
            expected, quota = batch_expected(r['inflight0'], r['inflight1'], r['bandwidth0'], r['bandwidth1'])
        else:
            scaled = [x * 16 for x in w]; quota = [int(x) for x in scaled]
            if policy:
                winner = 0 if scaled[0] - quota[0] >= scaled[1] - quota[1] else 1
            else:
                winner = 0 if w[0] >= w[1] else 1
            quota[winner] += 16 - sum(quota)
        if abs(w[0] - expected) > 1e-12 or [r['assigned0'], r['assigned1']] != [x * 65536 for x in quota]:
            raise ValueError('target weight or applied integer allocation differs')
        k = bisect.bisect_right(clock, r['ns']) - 1
        if k < 0:
            raise ValueError('decision precedes submission')
        q = req[k]
        if q['phase'] != 'measurement' or q['finished_ns'] < r['ns'] or q['request_id'] in used:
            raise ValueError('request correlation failed')
        used.add(q['request_id'])
        for j in (0, 1):
            d = r['dev' + str(j)]; name = m['environment']['nic_id_to_name'][str(d)]
            penalty = 1 if policy >= 2 or name == 'erdma_0' else 10
            if r['penalty' + str(j)] != penalty:
                raise ValueError('unexpected NUMA penalty')
            if policy >= 2:
                wanted = dev_seed[d]
                if policy in (3, 5):
                    index = bisect.bisect_right(rate_times[d], r['ns']) - 1
                    if index < 0:
                        raise ValueError('missing rate state before decision')
                    wanted = rate_events[d][index]['capacity']
                if not close(r['bandwidth' + str(j)], wanted):
                    raise ValueError('decision does not consume audited rate state')
            weights[(r['selector'], r['context'], (r['ns'] - start) // 50000000, d)].append(w[j])
    if len(selectors) != 1 or len(contexts) != 2 or {c[1] for c in contexts} != {1, 2}:
        raise ValueError('mixed contexts or missing probe')
    if any(b - a != 100 for a, b in zip(probes, probes[1:])):
        raise ValueError('probe cadence changed')
    eligible = [r for r in req if r['phase'] == 'measurement' and start <= r['submitted_ns'] < end]
    missing = [r for r in eligible if r['request_id'] not in used]
    if len(missing) > 1 or (missing and missing[0]['submitted_ns'] != max(r['submitted_ns'] for r in eligible)):
        raise ValueError('internal decision coverage gap')
    seen = set()
    for t in observer['threads']:
        definitions = {c['id']: c for c in t['contexts']}
        for row in t['rows']:
            if row['kind'] != 'weight':
                continue
            c = definitions[row['context']]; key = (c['selector'], c['id'], row['bin'], row['dev'])
            if key not in weights:
                continue
            v = weights[key]; value = row['values'][0]
            if key in seen or row['n'] != len(v) or abs(value['sum'] - sum(v)) > 1e-8 * len(v) or abs(value['last'] - v[-1]) > 1e-12:
                raise ValueError('independent observer count/weight mismatch')
            meaning = ('consumed_batch_target' if policy >= 4 else 'consumed_inverse_score') if c['mode'] == 1 else 'diagnostic_inverse_score_only'
            if c['weight_semantics'] != meaning:
                raise ValueError('weight semantics mislabeled')
            seen.add(key)
    if seen != set(weights):
        raise ValueError('missing observer crosscheck')
    stats = {str(d): dict(events=len(events), valid_updates=sum(e['valid'] for e in events),
        measurement_events=sum(start <= e['ns'] < end for e in events),
        measurement_valid=sum(e['valid'] for e in events if start <= e['ns'] < end)) for d, events in rate_events.items()}
    return dict(passed=True, records=len(rows), probes=len(probes), boundary_missing=len(missing),
        estimator=stats, whole_run_rate_counters=rate_meta['rails'],
        integer_solution_checked=True, observer_crosschecked=True), rows
