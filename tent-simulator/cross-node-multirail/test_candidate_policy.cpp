#include "candidate_policy.h"
#include <cassert>
#include <cmath>
#include <iostream>

int main() {
    using namespace tent_cand;
    for (int mode = 1; mode <= 3; ++mode) {
        State s; Snapshot v;
        assert(update(s, v, mode, .5, 1000000000, 1, 0, 1) == .5);
        assert(v.previous_ns == 0);
        for (int i = 1; i <= 1000; ++i) {
            const double w = update(s, v, mode, .5, 1000000000 + i * 1000000, 1, 0, 1);
            assert(w == .5);
        }
        for (int i = 1; i <= 100; ++i)
            update(s, v, mode, .9, 2000000000 + i * 1000000, 1, 0, 1);
        assert(std::abs(s.applied - .9) < (mode == 2 ? .02 : .001));
        assert(update(s, v, mode, .1, 2200000000, 1, 0, 1) == .1);
        assert(v.previous_ns == 0); // Idle reset.
        assert(update(s, v, mode, .8, 2201000000, 1, 0, 2) == .8);
        assert(v.previous_ns == 0); // Eligible set reset, no stale dead-rail share.
    }
    State s; Snapshot v;
    update(s, v, 2, .5, 1000000, 1, 0, 1);
    for (int i = 1; i < 100; ++i)
        assert(update(s, v, 2, i % 2 ? .51 : .49, 1000000 + i * 1000000, 1, 0, 1) == .5);
    for (unsigned n : {1U, 2U, 3U, 16U, 255U})
        for (int i = 0; i <= 10000; ++i) {
            uint32_t a, b;
            quotas(i / 10000., n, true, a, b);
            assert(a + b == n);
            assert(std::abs(a - i / 10000. * n) <= 1.000001);
        }
    uint32_t a, b;
    quotas(.5000000476836567, 16, false, a, b); assert(a == 9 && b == 7);
    quotas(.4999999523163615, 16, false, a, b); assert(a == 7 && b == 9);
    quotas(.5000000476836567, 16, true, a, b); assert(a == 8 && b == 8);
    quotas(.4999999523163615, 16, true, a, b); assert(a == 8 && b == 8);
    std::cout << "CANDIDATE_POLICY_TEST_OK\n";
}
