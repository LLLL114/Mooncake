#!/usr/bin/env python3
"""SSH-only detector and binary-record tests. Synthetic fixtures, not RDMA data."""
import argparse
import json
import math
from pathlib import Path
import subprocess

from oscillation_metrics import FORMAT, diagnose


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    base = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail').resolve()
    if base not in args.output.resolve().parents:
        raise ValueError('fixtures must be outside Git under experiment output')
    args.output.mkdir(parents=True, exist_ok=False)
    times = [i / 1000 for i in range(60000)]
    fixtures = {
        'constant': ([.5] * len(times), False, False),
        'monotone': ([.2 + .6 * t / 60 for t in times], False, False),
        'small_periodic': ([.5 + .01 * math.sin(t * 2 * math.pi * 5) for t in times], False, False),
        'one_spike': ([.9 if 25 <= t < 25.01 else .5 for t in times], False, False),
        'large_periodic': ([.5 + .2 * math.sin(t * 2 * math.pi * 5) for t in times], True, True),
        'biased_periodic': ([.75 + .15 * math.sin(t * 2 * math.pi * 3) for t in times], True, True),
        'only_startup': ([.5 + .2 * math.sin(t * 2 * math.pi * 5) if t < 10 else .5 for t in times], False, False),
        'only_one_block': ([.5 + .2 * math.sin(t * 2 * math.pi * 5) if 20 <= t < 30 else .5 for t in times], False, False),
    }
    results = {}
    for name, (values, sustained, periodic) in fixtures.items():
        d = diagnose(times, values)
        assert d['sustained_back_and_forth'] == sustained, name
        assert d['near_periodic'] == periodic, name
        results[name] = {k: d[k] for k in ('sustained_back_and_forth', 'near_periodic', 'full_cycles')}
    try:
        diagnose([10, 11], [.5])
    except ValueError:
        pass
    else:
        raise AssertionError('length mismatch not rejected')
    gapped_times = [10 + 2 * i + j * .01 for i in range(25) for j in range(2)]
    gapped_values = [v for i in range(25) for v in ((.2, .3) if i % 2 == 0 else (.8, .7))]
    assert diagnose(gapped_times, gapped_values)['full_cycles'] == 0
    assert diagnose(gapped_times, gapped_values)['gap_breaks'] == 24
    binary = {}
    for mode in ('basic', 'overflow', 'owners'):
        out = args.output / mode; out.mkdir()
        subprocess.run([str(args.binary), mode, str(out)], check=True)
        m = json.loads((out / 'oscillation-trace.json').read_text())
        assert m['record_bytes'] == 152 and m['fixed_equal'] and m['theoretical_override_gbps'] == 10
        assert m['owners'] == (1 if mode == 'overflow' else 5 if mode == 'owners' else 2)
        counts = []
        for slot in m['slots']:
            raw = (out / slot['file']).read_bytes()
            records = list(FORMAT.iter_unpack(raw))
            assert len(records) == slot['count'] == (2 if mode == 'overflow' else 4)
            assert slot['dropped'] == (2 if mode == 'overflow' else 0)
            assert [r[1] for r in records] == list(range(len(records)))
            assert all(r[2] == 1048576 and r[12:14] == (524288, 524288) and r[-2:] == (.25, .75) for r in records)
            counts.append(len(records))
        assert bool(m['error']) == (mode != 'basic')
        binary[mode] = dict(records=counts, error=m['error'], owners=m['owners'])
    (args.output / 'structural-results.json').write_text(json.dumps(dict(
        synthetic_only=True, detector=results, binary=binary, length_mismatch_rejected=True), indent=2))
    print('OSCILLATION_STRUCTURAL_TESTS_PASS', args.output, flush=True)


if __name__ == '__main__':
    main()
