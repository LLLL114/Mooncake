#pragma once
#include <array>
#include <cstdint>

namespace tent_sq {
struct Choice { double weight; uint32_t first; int32_t offset; };
struct Controller {
    std::array<std::array<uint32_t, 256>, 3> table{};
    double c0 = 0, c1 = 0, base = 0, sum = 0;
    uint64_t sequence = 0, window = 0, last = 0, samples = 0;
    int offset = 0, suggestion = 0, confirmations = 0;
    void init(double a, double b);
    Choice next(uint64_t now, uint64_t q0, uint64_t q1, bool bounded);
};
void bind(int dev, const char* name) noexcept;
void capture(int dev, uint64_t inflight) noexcept;
Choice choose(int dev0, int dev1, uint64_t ns) noexcept;
}
extern "C" int tent_sq_configure(int policy, double c0, double c1) noexcept;
extern "C" int tent_sq_policy() noexcept;
extern "C" int tent_sq_dump(const char* directory) noexcept;
