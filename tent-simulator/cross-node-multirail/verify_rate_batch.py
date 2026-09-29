#!/usr/bin/env python3
"""SSH-only checks of independent batch and rate-accounting assertions."""
import json
from pathlib import Path
from rate_batch_metrics import batch_expected, rates
from rate_batch_sender import parse_controls


def main():
    controls, rest = parse_controls(['--rate','707474227.2','--rate-batch-policy','1',
                                    '--rail0-bps','738721792','--rail1-bps','711808341.3333334'])
    assert controls.rate_batch_policy == 1 and rest == ['--rate','707474227.2']
    root = Path('/root/mooncake-tent-multirdma-output/cross-node-multirail/rate-batch-20260929')
    validation = root / 'validation'
    m = json.loads((validation / 'rate-estimates.json').read_text())
    assert len(m['rails']) == 2 and m['error'] == 0
    assert all(x['posted'] == x['done'] == 4000 * 65536 and x['pending'] == 0 for x in m['rails'])
    w, n = batch_expected(0, 4 * 65536, 1e9, 1e9)
    assert n == [10, 6] and w == .625
    w, n = batch_expected(1e9, 0, 1e9, 1e9)
    assert n == [0, 16] and w == 0
    # Decode complete counter dump and reject inconsistent whole-run byte totals.
    manifest = dict(total=dict(accepted=500), environment=dict(nic_id_to_name={'0':'erdma_0','1':'erdma_1'}))
    rates(validation, 3, [1e9,1e9], manifest)
    manifest['total']['accepted'] = 1
    try:
        rates(validation, 3, [1e9,1e9], manifest)
    except ValueError as e:
        assert 'accounting mismatch' in str(e)
    else:
        raise AssertionError('bad byte accounting accepted')
    print('RB_PYTHON_CHECK_OK')


if __name__ == '__main__':
    main()
