#include "candidate_policy.h"
#include <atomic>
#include <cmath>
#include <cerrno>

namespace {
std::atomic<int> policy{0};
std::atomic<bool> configured{false};
thread_local tent_cand::State state;
thread_local tent_cand::Snapshot last;
}
namespace tent_cand {
double update(State& s, Snapshot& out, int kind, double raw, uint64_t now,
              uintptr_t selector, int d0, int d1) noexcept {
    const bool restart = !s.ns || now <= s.ns || now - s.ns >= 100000000 ||
                         s.selector != selector || s.dev0 != d0 || s.dev1 != d1;
    out = {s.filtered, s.applied, s.filtered, s.ns, now};
    if (restart || kind == 0 || kind == 3) {
        s.filtered = s.applied = raw;
    } else {
        const double dt = double(now - s.ns);
        const double gain = dt / (10000000.0 + dt); // 10ms first-order filter.
        s.filtered += gain * (raw - s.filtered);
        if (kind == 1 || std::abs(s.filtered - s.applied) >= .02)
            s.applied = s.filtered;
    }
    // Zero previous_ns makes resets explicit to the independent trace audit.
    if (restart) out.previous_ns = 0;
    out.filtered = s.filtered;
    s.ns = now; s.selector = selector; s.dev0 = d0; s.dev1 = d1;
    return s.applied;
}
double choose(double raw, uint64_t ns, const void* selector, int d0, int d1) noexcept {
    return update(state, last, tent_cand_policy(), raw, ns,
                  reinterpret_cast<uintptr_t>(selector), d0, d1);
}
Snapshot snapshot() noexcept { return last; }
void reset() noexcept { state = {}; last = {}; }
void quotas(double w, uint32_t n, bool remainder, uint32_t& a, uint32_t& b) noexcept {
    const double x = w * n, y = (1.0 - w) * n;
    a = static_cast<uint32_t>(x); b = static_cast<uint32_t>(y);
    const uint32_t left = n - a - b;
    const bool first = remainder ? (x - a >= y - b) : (w >= .5);
    if (first) a += left; else b += left;
}
}
extern "C" int tent_cand_configure(int value) noexcept {
    if (value < 0 || value > 3) return -EINVAL;
    if (configured.exchange(true)) return -EALREADY;
    policy.store(value, std::memory_order_relaxed);
    return 0;
}
extern "C" int tent_cand_policy() noexcept { return policy.load(std::memory_order_relaxed); }
