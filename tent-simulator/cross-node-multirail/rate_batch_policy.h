#pragma once
#include <cstdint>

namespace tent_rb {
struct Input { uint64_t inflight = 0; double capacity = 0; };
struct Estimate {
    uint64_t ns, dev, done, previous_done, interval, send_span, minimum_pending, valid, reason;
    double previous_capacity, observed_rate, capacity;
};
static_assert(sizeof(Estimate) == 96);
struct Learning {
    double capacity = 0;
    uint64_t ns = 0, done = 0, send = 0, errors = 0;
};
struct Choice { double target; uint32_t first, second; double finish; };
Estimate learn(Learning& state, uint64_t ns, uint64_t done, uint64_t send,
               uint64_t minimum_pending, uint64_t errors, double seed) noexcept;
Choice batch(uint64_t q0, uint64_t q1, double c0, double c1,
             uint32_t slices, uint64_t slice_bytes) noexcept;
void remainder(double w0, double w1, uint32_t n, uint32_t& a, uint32_t& b) noexcept;
void bind(int dev, const char* name) noexcept;
void posted(int dev, uint64_t bytes) noexcept;
void completed(int dev, uint64_t bytes, uint64_t submit_ns, uint64_t poll_ns, bool valid) noexcept;
double capacity(int dev, uint64_t now) noexcept;
void capture(int dev, uint64_t inflight, double capacity) noexcept;
Input input(int dev) noexcept;
}
extern "C" int tent_rb_configure(int policy, double rail0_bps, double rail1_bps) noexcept;
extern "C" int tent_rb_policy() noexcept;
extern "C" int tent_rb_dump(const char* directory) noexcept;
