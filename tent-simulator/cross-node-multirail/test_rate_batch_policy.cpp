#include "rate_batch_policy.h"
#include <algorithm>
#include <cassert>
#include <cmath>
#include <iostream>
#include <thread>
#include <vector>

int main(int argc, char** argv) {
    using namespace tent_rb;
    // Independent exhaustive optimum for varied queues, rates, counts and sizes.
    for (uint32_t n : {1U, 2U, 3U, 16U, 127U})
      for (uint64_t bytes : {1ULL, 4096ULL, 65536ULL})
       for (uint64_t a : {0ULL, 1ULL, 4ULL, 40ULL})
        for (uint64_t b : {0ULL, 3ULL, 17ULL})
         for (double c0 : {1e7, 7.3e8, 1e10})
          for (double c1 : {1e7, 2.1e8, 1e10}) {
            const auto s = batch(a * bytes, b * bytes, c0, c1, n, bytes);
            double best = INFINITY;
            for (uint32_t k = 0; k <= n; ++k) {
                const double t0 = k ? double((a + k) * bytes) / c0 : 0;
                const double t1 = k < n ? double((b + n - k) * bytes) / c1 : 0;
                best = std::min(best, std::max(t0, t1));
            }
            assert(s.first + s.second == n);
            assert(std::abs(s.finish - best) <= 1e-12 * std::max(1., best));
          }
    auto q = batch(0, 4 * 65536, 1e9, 1e9, 16, 65536);
    assert(q.first == 10 && q.second == 6);
    q = batch(1000000000, 0, 1e9, 1e9, 16, 65536);
    assert(q.first == 0 && q.second == 16);
    Learning s;
    auto r = learn(s, 1000000, 0, 0, 0, 0, 1e9);
    assert(r.reason == 1 && s.capacity == 1e9);
    r = learn(s, 6000000, 2500000, 5000000, 200000, 0, 1e9);
    assert(r.valid && std::abs(s.capacity - 9e8) < 1e-6);
    const double before = s.capacity;
    r = learn(s, 11000000, 2600000, 10000000, 0, 0, 1e9);
    assert(!r.valid && s.capacity == before && (r.reason & 8));
    r = learn(s, 16000000, 7600000, 15000000, 300000, 1, 1e9);
    assert(!r.valid && s.capacity == before && (r.reason & 16));
    // Large delivery bursts bounded to 2x the independently calibrated seed.
    r = learn(s, 21000000, 100000000, 20000000, 300000, 1, 1e9);
    assert(r.valid && s.capacity <= 2e9);
    assert(tent_rb_configure(3, 1e9, 1e9) == 0);
    bind(0, "erdma_0"); bind(1, "erdma_1");
    std::vector<std::thread> threads;
    for (int t = 0; t < 8; ++t) threads.emplace_back([t] {
        for (int i = 0; i < 1000; ++i) {
            posted(t % 2, 65536);
            completed(t % 2, 65536, 1000000 + i, 2000000 + i, true);
        }
    });
    for (auto& thread : threads) thread.join();
    assert(capacity(0, 3000000) == 1e9);
    assert(capacity(1, 3000000) == 1e9);
    if (argc == 2) assert(tent_rb_dump(argv[1]) == 0);
    std::cout << "RATE_BATCH_POLICY_TEST_OK\n";
}
