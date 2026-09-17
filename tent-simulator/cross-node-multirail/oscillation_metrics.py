"""Stdlib-only trace validation and pre-registered directional-change metrics."""
import bisect
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics
import struct

FORMAT = struct.Struct('<4Q4I2i4Q8d')
FIELDS = ('ns sequence total_bytes selector slices mode context site dev0 dev1 '
          'inflight0 inflight1 assigned0 assigned1 score0 score1 bandwidth0 bandwidth1 '
          'penalty0 penalty1 weight0 weight1').split()
assert FORMAT.size == 152 and len(FIELDS) == 22


def read_json(path):
    return json.loads(Path(path).read_text())


def quantile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def describe(values):
    values = [v for v in values if v is not None]
    if not values:
        return dict(n=0, median=None, mean=None, sd=None, cv=None, p05=None, p95=None)
    mean, sd = statistics.mean(values), statistics.pstdev(values)
    return dict(n=len(values), median=statistics.median(values), mean=mean, sd=sd,
                cv=sd / mean if mean else None, p05=quantile(values, .05), p95=quantile(values, .95),
                minimum=min(values), maximum=max(values))


def pivots(times, values, threshold):
    """Confirm a reversal only after retreating threshold from a running extreme."""
    if len(times) != len(values) or threshold <= 0:
        raise ValueError('invalid directional-change input')
    if not values:
        return []
    if not math.isfinite(values[0]) or not math.isfinite(times[0]):
        raise ValueError('invalid first sample')
    lo = hi = values[0]
    low_index = high_index = 0
    direction = 0
    result = []

    def append(index, value, kind, confirmed):
        result.append(dict(time=times[index], value=value, kind=kind, confirmed_at=times[confirmed]))

    for i, value in enumerate(values[1:], 1):
        if times[i] <= times[i - 1] or not math.isfinite(value):
            raise ValueError('nonmonotonic clock or invalid value')
        if direction == 0:
            if value < lo:
                lo, low_index = value, i
            if value > hi:
                hi, high_index = value, i
            if value - lo >= threshold:
                append(low_index, lo, 'trough', i)
                direction, hi, high_index = 1, value, i
            elif hi - value >= threshold:
                append(high_index, hi, 'peak', i)
                direction, lo, low_index = -1, value, i
        elif direction > 0:
            if value > hi:
                hi, high_index = value, i
            elif hi - value >= threshold:
                append(high_index, hi, 'peak', i)
                direction, lo, low_index = -1, value, i
        else:
            if value < lo:
                lo, low_index = value, i
            elif value - lo >= threshold:
                append(low_index, lo, 'trough', i)
                direction, hi, high_index = 1, value, i
    return result


def full_cycles(extrema):
    result = []
    # Entry 0 only establishes the initial direction; it may lie on the
    # analysis boundary rather than at an observed turning point.
    for i in range(1, len(extrema) - 2, 2):
        a, b, c = extrema[i:i + 3]
        if a['kind'] != c['kind'] or a['kind'] == b['kind']:
            raise ValueError('non-alternating extrema')
        result.append(dict(start=a['time'], end=c['time'], confirmed_at=c['confirmed_at'],
                           period=c['time'] - a['time'],
                           amplitude=min(abs(b['value'] - a['value']), abs(c['value'] - b['value']))))
    return result


def segmented_turns(times, values, threshold, max_gap):
    splits = [0] + [i for i in range(1, len(times)) if max_gap is not None and times[i] - times[i - 1] > max_gap] + [len(times)]
    extrema, cycles, reversals = [], [], 0
    for segment, (left, right) in enumerate(zip(splits, splits[1:])):
        current = pivots(times[left:right], values[left:right], threshold)
        for point in current:
            point['segment'] = segment
        extrema.extend(current)
        cycles.extend(full_cycles(current))
        reversals += max(0, len(current) - 1)
    return extrema, cycles, reversals, max(0, len(splits) - 2)


def diagnose(times, values, start=10., end=60., threshold=.05, max_gap=1.):
    if end <= start or len(times) != len(values):
        raise ValueError('invalid interval')
    selected = [(t, v) for t, v in zip(times, values) if start <= t < end]
    t = [x[0] for x in selected]; v = [x[1] for x in selected]
    extrema, cycles, reversals, gap_breaks = segmented_turns(t, v, threshold, max_gap)
    blocks = []
    left = start
    while left + 10 <= end + 1e-9:
        block = [(x, y) for x, y in selected if left <= x < left + 10]
        count = len(segmented_turns([x for x, _ in block], [y for _, y in block], threshold, max_gap)[1])
        blocks.append(dict(start=left, cycles=count, passes=count >= 3))
        left += 10
    stats = describe(v)
    spread = stats['p95'] - stats['p05'] if v else None
    repeated = (spread is not None and spread >= .10 and len(cycles) >= 10
                and len(blocks) == 5 and sum(b['passes'] for b in blocks) >= 4)
    periods = describe([c['period'] for c in cycles])
    thirds = []
    for i in range(3):
        left, right = start + (end - start) * i / 3, start + (end - start) * (i + 1) / 3
        thirds.append(describe([c['period'] for c in cycles if left <= c['start'] and c['end'] < right]))
    medians = [x['median'] for x in thirds if x['n'] >= 3]
    periodic = bool(repeated and periods['cv'] is not None and periods['cv'] <= .25
                    and len(medians) == 3 and min(medians) > 0 and max(medians) / min(medians) <= 1.2)
    return dict(samples=len(v), threshold=threshold, p95_p05=spread, values=stats,
                reversals=reversals, full_cycles=len(cycles), gap_breaks=gap_breaks,
                cycles_per_second=len(cycles) / (end - start), blocks=blocks,
                sustained_back_and_forth=bool(repeated), near_periodic=periodic,
                periods=periods, period_thirds=thirds, extrema=extrema, cycles=cycles)


def load_trace(root):
    root = Path(root)
    meta = read_json(root / 'oscillation-trace.json')
    if meta['schema'] != 'tent-oscillation-trace-v1' or meta['record_bytes'] != FORMAT.size or meta['error']:
        raise ValueError('invalid trace metadata or recorded error')
    rows = []
    for slot in meta['slots']:
        if slot['dropped'] or Path(slot['file']).name != slot['file']:
            raise ValueError('trace overflow or invalid file name')
        raw = (root / slot['file']).read_bytes()
        if len(raw) != slot['count'] * FORMAT.size:
            raise ValueError('trace length mismatch')
        for i, entry in enumerate(FORMAT.iter_unpack(raw)):
            row = dict(zip(FIELDS, entry)); row['owner'] = slot['owner']
            if row['sequence'] != i:
                raise ValueError('missing trace sequence')
            rows.append(row)
    if not rows or len({x['owner'] for x in rows}) != 1:
        raise ValueError('protocol requires exactly one nonempty decision producer')
    rows.sort(key=lambda r: r['ns'])
    if any(a['ns'] >= b['ns'] for a, b in zip(rows, rows[1:])):
        raise ValueError('decision clock is not strictly increasing')
    return meta, rows


def audit_trace(root, fixed_equal, symmetric_prior):
    root = Path(root)
    manifest = read_json(root / 'manifest.json')
    observer = read_json(root / 'observer.json')
    if observer['incomplete'] or manifest['data_verified'] is not True:
        raise ValueError('incomplete observation or unverified data')
    meta, rows = load_trace(root)
    if meta['fixed_equal'] != fixed_equal or meta['theoretical_override_gbps'] != (10 if symmetric_prior else 0):
        raise ValueError('trace control differs from protocol')
    start, end = manifest['measurement_start_ns'], manifest['measurement_end_ns']
    names = manifest['environment']['nic_id_to_name']
    requests = [json.loads(line) for line in (root / 'requests.jsonl').read_text().splitlines()]
    if any(r['status'] != 'success' for r in requests):
        raise ValueError('failed request')
    submitted = sorted(requests, key=lambda r: r['submitted_ns'])
    clock = [r['submitted_ns'] for r in submitted]
    used = set()
    weights = defaultdict(list)
    selectors = set(); contexts = set(); probe_positions = []
    for i, r in enumerate(rows):
        if not start <= r['ns'] < end or r['site'] != 1 or r['mode'] not in (1, 2):
            raise ValueError('unexpected trace scope')
        if r['total_bytes'] != 1048576 or r['slices'] != 16 or r['dev0'] >= r['dev1']:
            raise ValueError('unexpected request/slice/device layout')
        selectors.add(r['selector']); contexts.add((r['context'], r['mode']))
        if r['mode'] == 2:
            probe_positions.append(i)
        w = [r['weight0'], r['weight1']]
        score = [r['score0'], r['score1']]
        inv = [1 / (s + 1e-12) for s in score]
        if any(not math.isfinite(x) or x < 0 or x > 1 for x in w) or abs(sum(w) - 1) > 1e-12:
            raise ValueError('invalid normalized weight')
        if any(abs(w[j] - inv[j] / sum(inv)) > 1e-12 for j in (0, 1)):
            raise ValueError('weight does not correspond to captured original score')
        allocation = [r['assigned0'], r['assigned1']]
        expected = [8, 8] if fixed_equal or r['mode'] == 2 else [int(x * 16) for x in w]
        if not fixed_equal and r['mode'] != 2:
            expected[0 if inv[0] >= inv[1] else 1] += 16 - sum(expected)
        if allocation != [n * 65536 for n in expected]:
            raise ValueError('applied allocation differs from fixed/probe/original rounding rule')
        index = bisect.bisect_right(clock, r['ns']) - 1
        if index < 0:
            raise ValueError('decision precedes submission')
        request = submitted[index]
        if request['phase'] != 'measurement' or request['finished_ns'] < r['ns'] or request['request_id'] in used:
            raise ValueError('decision/request temporal association is invalid')
        used.add(request['request_id'])
        r['request_id'] = request['request_id']
        bin_index = (r['ns'] - start) // 50000000
        for j in (0, 1):
            name = names[str(r['dev' + str(j)])]
            expected_penalty = 1 if symmetric_prior or name == 'erdma_0' else 10
            if r['penalty' + str(j)] != expected_penalty:
                raise ValueError('NUMA penalty mismatch')
            weights[(r['selector'], r['context'], bin_index, r['dev' + str(j)])].append(w[j])
    if len(selectors) != 1 or {x[1] for x in contexts} != {1, 2} or len(contexts) != 2:
        raise ValueError('multiple incompatible decision contexts or missing probe')
    if any(b - a != 100 for a, b in zip(probe_positions, probe_positions[1:])):
        raise ValueError('probe cadence differs from the original algorithm')
    eligible = {r['request_id']: r for r in requests if r['phase'] == 'measurement' and start <= r['submitted_ns'] < end}
    missing = [r for key, r in eligible.items() if key not in used]
    if len(missing) > 1 or (missing and missing[0]['submitted_ns'] != max(x['submitted_ns'] for x in eligible.values())):
        raise ValueError('internal trace coverage gap')
    seen = set()
    for thread in observer['threads']:
        definitions = {c['id']: c for c in thread['contexts']}
        for row in thread['rows']:
            if row['kind'] != 'weight':
                continue
            c = definitions[row['context']]
            key = (c['selector'], c['id'], row['bin'], row['dev'])
            if key not in weights:
                continue
            data = weights[key]
            if key in seen or row['n'] != len(data):
                raise ValueError('observer/trace count mismatch')
            value = row['values'][0]
            if abs(value['sum'] - sum(data)) > 1e-8 * len(data) or abs(value['last'] - data[-1]) > 1e-12:
                raise ValueError('observer/trace weight mismatch')
            wanted = ('shadow_inverse_score' if fixed_equal else 'consumed_inverse_score') if c['mode'] == 1 else 'diagnostic_inverse_score_only'
            if c['weight_semantics'] != wanted:
                raise ValueError('weight meaning mislabeled')
            seen.add(key)
    if seen != set(weights):
        raise ValueError('missing independent 50ms observer cross-check')
    return dict(passed=True, records=len(rows), normal=sum(r['mode'] == 1 for r in rows),
                probes=len(probe_positions), boundary_missing=len(missing),
                fixed_equal=fixed_equal, symmetric_prior=symmetric_prior,
                exact_allocation_checked=True, observer_weights_cross_checked=True), rows
