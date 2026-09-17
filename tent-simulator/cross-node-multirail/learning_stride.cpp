#include "learning_stride.h"

#include <array>
#include <atomic>
#include <cerrno>

namespace {
std::atomic<unsigned> stride{1};
std::atomic<int> error{0};
static_assert(std::atomic<unsigned>::is_always_lock_free);
}

extern "C" int tent_e_set_stride(unsigned value) noexcept {
    if (value == 0 || value > 1000000) return -EINVAL;
    stride.store(value, std::memory_order_relaxed);
    return 0;
}

extern "C" int tent_e_error() noexcept {
    return error.load(std::memory_order_relaxed);
}

bool tent_e_should_learn(int device) noexcept {
    const unsigned k = stride.load(std::memory_order_relaxed);
    static thread_local unsigned previous = 1;
    if (k == 1) {
        previous = 1;
        return true;
    }
    if (device < 0 || device >= 64) {
        error.store(-ERANGE, std::memory_order_relaxed);
        return true; // Preserve transmission; the controller rejects this run.
    }
    static thread_local std::array<unsigned, 64> counts{};
    if (previous != k) {
        counts.fill(0);
        previous = k;
    }
    unsigned& count = counts[device];
    if (++count == k) {
        count = 0;
        return true;
    }
    return false;
}
