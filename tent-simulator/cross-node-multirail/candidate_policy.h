#pragma once
#include <cstdint>

namespace tent_cand {
// Experimental single-caller, two-rail state. Eligibility is checked by TENT.
struct Snapshot {
    double previous_filtered = 0, previous_applied = 0, filtered = 0;
    uint64_t previous_ns = 0, decision_ns = 0;
};
struct State {
    uintptr_t selector = 0;
    int dev0 = -1, dev1 = -1;
    double filtered = 0, applied = 0;
    uint64_t ns = 0;
};
double update(State& state, Snapshot& snapshot, int policy, double raw,
              uint64_t ns, uintptr_t selector, int dev0, int dev1) noexcept;
double choose(double raw, uint64_t ns, const void* selector, int dev0, int dev1) noexcept;
Snapshot snapshot() noexcept;
void reset() noexcept;
void quotas(double weight0, uint32_t slices, bool largest_remainder,
            uint32_t& n0, uint32_t& n1) noexcept;
}
extern "C" int tent_cand_configure(int policy) noexcept;
extern "C" int tent_cand_policy() noexcept;
