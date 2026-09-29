#include "stable_quota.h"
#include <cassert>
#include <cmath>
#include <iostream>

int main() {
    using tent_sq::Controller;
    for (double a : {1., 10., 100., 738721792.}) for (double b : {1., 10., 711808341.3333334}) {
        Controller s; s.init(a, b);
        unsigned total = 0;
        for (unsigned i = 0; i < 256; ++i) {
            auto p = s.next(1 + i, i * 1000000, 0, false);
            assert(p.first <= 16 && p.offset == 0 && p.weight == s.base);
            total += p.first;
        }
        assert(std::abs(double(total) / 4096 - a / (a + b)) <= .5 / 4096 + 1e-15);
        for (const auto& row : s.table) for (auto n : row) assert(n <= 16);
    }
    Controller s; s.init(1e9, 1e9);
    for (unsigned i = 0; i <= 60; ++i) {
        auto p = s.next(1 + i * 10000000ULL, 10000000, 0, true);
        assert(p.offset == (i < 30 ? 0 : -1));
        assert(p.first == (i < 30 ? 8u : 7u));
    }
    auto p = s.next(1000000001, 0, 0, true);
    assert(p.offset == 0 && p.first == 8);
    for (unsigned i = 1; i <= 60; ++i) {
        p = s.next(1000000001 + i * 10000000ULL, 0, 10000000, true);
        assert(p.offset >= 0 && p.offset <= 1 && p.first <= 9);
    }
    assert(p.offset == 1);
    std::cout << "STABLE_QUOTA_TEST_OK\n";
}
