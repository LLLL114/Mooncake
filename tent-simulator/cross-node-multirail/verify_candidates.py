#!/usr/bin/env python3
"""Independent arithmetic and rejection checks; execute on SSH server only."""
from candidate_metrics import expected_weight, expected_quota


def sample(raw, previous=.5, applied=.5, dt=1000000):
    # epsilon-adjusted scores yield the desired original normalized weight.
    return dict(score0=1 / raw - 1e-12, score1=1 / (1 - raw) - 1e-12,
        mode=1, ns=1000000000 + dt, decision_ns=1000000000 + dt,
        previous_ns=1000000000, previous_filtered=previous, previous_applied=applied,
        filtered=previous + dt / (10000000 + dt) * (raw - previous))


def main():
    r = sample(.8)
    raw, applied = expected_weight(r, 1)
    assert abs(raw - .8) < 1e-12 and abs(applied - (.5 + .3 / 11)) < 1e-12
    r = sample(.6)
    assert expected_weight(r, 2)[1] == .5
    r = sample(.8)
    assert abs(expected_weight(r, 2)[1] - (.5 + .3 / 11)) < 1e-12
    r['filtered'] += .01
    try:
        expected_weight(r, 1)
    except ValueError:
        pass
    else:
        raise AssertionError('corrupt recurrence accepted')
    r = sample(.8); r['previous_ns'] = 0; r['filtered'] = .8
    assert abs(expected_weight(r, 1)[1] - .8) < 1e-12
    for w, old in ((.5000000476836567, [9, 7]), (.4999999523163615, [7, 9])):
        assert expected_quota(w, 0) == old
        assert expected_quota(w, 3) == [8, 8]
    assert expected_quota(.99, 2, probe=True) == [8, 8]
    print('CANDIDATE_ANALYSIS_TEST_OK')


if __name__ == '__main__':
    main()
