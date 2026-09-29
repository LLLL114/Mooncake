#include "stable_quota.h"
#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <fstream>
#include <memory>
#include <string>

namespace tent_sq {
void Controller::init(double a, double b) {
    *this = Controller{};
    c0 = a; c1 = b;
    const uint32_t mass = uint32_t(std::llround(a / (a + b) * 4096));
    base = double(mass) / 4096;
    for (int j = 0; j < 3; ++j) {
        const uint32_t total = uint32_t(std::clamp(int(mass) + (j - 1) * 256, 0, 4096));
        for (uint32_t i = 0; i < 256; ++i)
            table[j][i] = ((i + 1) * total) / 256 - (i * total) / 256;
    }
}
Choice Controller::next(uint64_t now, uint64_t q0, uint64_t q1, bool bounded) {
    if (bounded) {
        if (!last || now - last > 200000000) {
            window = now; sum = 0; samples = 0;
            offset = suggestion = confirmations = 0;
        }
        last = now;
        sum += double(q0) / c0 - double(q1) / c1;
        ++samples;
        if (now - window >= 100000000) {
            const double mean = sum / samples, threshold = 1048576. / std::min(c0, c1);
            const int requested = mean > threshold ? -1 : mean < -threshold ? 1 : 0;
            confirmations = requested == suggestion ? std::min(3, confirmations + 1) : 1;
            suggestion = requested;
            if (confirmations == 3) offset = requested;
            window = now; sum = 0; samples = 0;
        }
    }
    const auto count = table[offset + 1][sequence++ % 256];
    return {std::clamp(base + double(offset) / 16, 0., 1.), count, offset};
}
}
namespace {
// Experimental scope: one selector and allocation caller, two fixed named NICs.
constexpr unsigned Limit = 200000;
struct Record { uint64_t ns, q0, q1, sequence; double weight; uint32_t first; int32_t offset; };
static_assert(sizeof(Record) == 48);
tent_sq::Controller state;
std::unique_ptr<Record[]> records;
uint32_t count = 0;
uint64_t queues[64]{};
int rails[2] = {-1, -1}, policy = 0, error = 0;
bool configured = false, dumped = false;
}
namespace tent_sq {
void bind(int d, const char* name) noexcept {
    const int rail = std::strcmp(name, "erdma_0") == 0 ? 0 : std::strcmp(name, "erdma_1") == 0 ? 1 : -1;
    if (d < 0 || d >= 64 || rail < 0 || (rails[rail] >= 0 && rails[rail] != d)) { error = EINVAL; return; }
    rails[rail] = d;
}
void capture(int d, uint64_t q) noexcept {
    if (d < 0 || d >= 64) { error = EINVAL; return; }
    queues[d] = q;
}
Choice choose(int a, int b, uint64_t now) noexcept {
    if (a != rails[0] || b != rails[1]) { error = EINVAL; return {.5, 8, 0}; }
    const auto seq = state.sequence;
    const auto choice = state.next(now, queues[a], queues[b], policy == 2);
    if (count < Limit) records[count++] = {now, queues[a], queues[b], seq, choice.weight, choice.first, choice.offset};
    else error = EOVERFLOW;
    return choice;
}
}
extern "C" int tent_sq_configure(int p, double a, double b) noexcept {
    if (configured || p < 1 || p > 2 || !std::isfinite(a) || !std::isfinite(b) || a <= 0 || b <= 0) return -EINVAL;
    try { records = std::make_unique<Record[]>(Limit); } catch (...) { return -ENOMEM; }
    configured = true; policy = p; state.init(a, b); return 0;
}
extern "C" int tent_sq_policy() noexcept { return policy; }
extern "C" int tent_sq_dump(const char* directory) noexcept {
    if (!directory || dumped || !configured) return -EINVAL;
    dumped = true;
    try {
        const std::string root(directory);
        std::ofstream binary(root + "/stable-quota.bin", std::ios::binary);
        binary.write(reinterpret_cast<const char*>(records.get()), sizeof(Record) * count);
        binary.close();
        std::ofstream meta(root + "/stable-quota.json");
        meta.precision(17);
        meta << "{\"schema\":\"tent-stable-quota-v1\",\"format\":\"<4QdIi\",\"record_bytes\":48,\"count\":"
             << count << ",\"error\":" << error << ",\"policy\":" << policy
             << ",\"capacity\":[" << state.c0 << ',' << state.c1 << "],\"base\":" << state.base
             << ",\"rails\":[" << rails[0] << ',' << rails[1] << "]}\n";
        meta.close();
        return binary && meta ? -error : -EIO;
    } catch (...) { return -EIO; }
}
