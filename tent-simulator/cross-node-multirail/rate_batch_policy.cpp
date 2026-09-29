#include "rate_batch_policy.h"
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <fstream>
#include <limits>
#include <memory>
#include <string>

namespace {
constexpr unsigned Devices = 64, MaxEstimates = 65536;
struct alignas(128) Counter {
    std::atomic<uint64_t> posted{0}, done{0}, last_send{0}, errors{0};
    std::atomic<int64_t> pending{0};
    std::atomic<uint64_t> minimum{UINT64_MAX};
    int rail = -1;
};
Counter counters[Devices];
tent_rb::Learning learning[Devices]; // Only the single allocation caller writes.
thread_local tent_rb::Input inputs[Devices];
std::unique_ptr<tent_rb::Estimate[]> records;
uint32_t count = 0;
double seeds[2]{};
std::atomic<int> policy{0}, error{0};
std::atomic<bool> configured{false};
bool dumped = false;
void reject(int e) { int zero = 0; error.compare_exchange_strong(zero, e); }
bool known(int d) { return d >= 0 && d < int(Devices) && counters[d].rail >= 0; }
void lower(std::atomic<uint64_t>& a, uint64_t v) {
    auto old = a.load(std::memory_order_relaxed);
    while (v < old && !a.compare_exchange_weak(old, v, std::memory_order_relaxed)) {}
}
void higher(std::atomic<uint64_t>& a, uint64_t v) {
    auto old = a.load(std::memory_order_relaxed);
    while (v > old && !a.compare_exchange_weak(old, v, std::memory_order_relaxed)) {}
}
}

namespace tent_rb {
Estimate learn(Learning& s, uint64_t now, uint64_t done, uint64_t send,
               uint64_t pending, uint64_t errors, double seed) noexcept {
    Estimate r{};
    r.ns = now; r.done = done; r.previous_done = s.done;
    r.previous_capacity = s.capacity ? s.capacity : seed;
    r.minimum_pending = pending;
    if (!s.ns) {
        s = {seed, now, done, send, errors};
        r.reason = 1; r.capacity = seed; return r;
    }
    r.interval = now - s.ns;
    r.send_span = send >= s.send ? send - s.send : 0;
    const uint64_t duration = std::max(r.interval, r.send_span);
    const uint64_t bytes = done >= s.done ? done - s.done : 0;
    r.observed_rate = duration ? double(bytes) * 1e9 / duration : 0;
    // Reason is a bit mask; low-load evidence must never lower the seed.
    if (r.interval < 5000000) r.reason |= 2;
    if (bytes < 262144) r.reason |= 4;
    if (pending < 131072 || pending == UINT64_MAX) r.reason |= 8;
    if (errors != s.errors || done < s.done || send < s.send) r.reason |= 16;
    r.valid = r.reason == 0;
    if (r.valid) {
        // Finite, bounded experimental estimator; 20ms response scale.
        const double observed = std::clamp(r.observed_rate, seed * .05, seed * 2.);
        const double gain = double(r.interval) / (20000000. + r.interval);
        s.capacity += gain * (observed - s.capacity);
    }
    s.ns = now; s.done = done; s.send = send; s.errors = errors;
    r.capacity = s.capacity;
    return r;
}

Choice batch(uint64_t q0, uint64_t q1, double c0, double c1,
             uint32_t n, uint64_t bytes) noexcept {
    if (!n || !bytes || !(c0 > 0) || !(c1 > 0)) return {};
    const double size = double(n) * bytes;
    const double target = std::clamp((c0 * size + c0 * double(q1) - c1 * double(q0)) /
                                     (c0 + c1), 0., size) / bytes;
    uint32_t points[4] = {0, n, uint32_t(std::floor(target)), uint32_t(std::ceil(target))};
    Choice result{target / n, 0, n, std::numeric_limits<double>::infinity()};
    double best_distance = std::numeric_limits<double>::infinity();
    for (uint32_t k : points) {
        const double a = k ? (double(q0) + double(k) * bytes) / c0 : 0;
        const double b = k < n ? (double(q1) + double(n - k) * bytes) / c1 : 0;
        const double finish = std::max(a, b), distance = std::abs(double(k) - target);
        if (finish < result.finish || (finish == result.finish &&
            (distance < best_distance || (distance == best_distance && k < result.first)))) {
            result.first = k; result.second = n - k; result.finish = finish; best_distance = distance;
        }
    }
    return result;
}
void remainder(double a, double b, uint32_t n, uint32_t& x, uint32_t& y) noexcept {
    const double first = a * n, second = b * n;
    x = uint32_t(first); y = uint32_t(second);
    const auto extra = n - x - y;
    if (first - x >= second - y) x += extra; else y += extra;
}
void bind(int d, const char* name) noexcept {
    int rail = std::strcmp(name, "erdma_0") == 0 ? 0 : std::strcmp(name, "erdma_1") == 0 ? 1 : -1;
    if (d < 0 || d >= int(Devices) || rail < 0) { reject(EINVAL); return; }
    if (counters[d].rail >= 0 && counters[d].rail != rail) { reject(EINVAL); return; }
    counters[d].rail = rail;
    learning[d].capacity = seeds[rail];
}
void posted(int d, uint64_t bytes) noexcept {
    if (!known(d)) { reject(EINVAL); return; }
    counters[d].posted.fetch_add(bytes, std::memory_order_relaxed);
    counters[d].pending.fetch_add(bytes, std::memory_order_relaxed);
}
void completed(int d, uint64_t bytes, uint64_t submit, uint64_t poll, bool valid) noexcept {
    if (!known(d)) { reject(EINVAL); return; }
    auto& c = counters[d];
    auto old = c.pending.fetch_sub(bytes, std::memory_order_relaxed);
    if (old < int64_t(bytes)) { reject(ERANGE); return; }
    lower(c.minimum, uint64_t(old - bytes));
    if (!valid || !submit || poll < submit) {
        c.errors.fetch_add(1, std::memory_order_relaxed); return;
    }
    higher(c.last_send, submit);
    c.done.fetch_add(bytes, std::memory_order_release);
}
double capacity(int d, uint64_t now) noexcept {
    if (!known(d)) { reject(EINVAL); return 1.; }
    auto& c = counters[d]; auto& s = learning[d]; const double seed = seeds[c.rail];
    if (tent_rb_policy() != 3 && tent_rb_policy() != 5) return seed;
    if (s.ns && now - s.ns < 5000000) return s.capacity;
    const auto done = c.done.load(std::memory_order_acquire);
    const auto send = c.last_send.load(std::memory_order_relaxed);
    const auto pending = c.pending.load(std::memory_order_relaxed);
    const auto minimum = c.minimum.exchange(pending >= 0 ? uint64_t(pending) : 0, std::memory_order_relaxed);
    auto r = learn(s, now, done, send, minimum, c.errors.load(std::memory_order_relaxed), seed);
    r.dev = uint64_t(d);
    if (count < MaxEstimates) records[count++] = r; else reject(EOVERFLOW);
    return s.capacity;
}
void capture(int d, uint64_t q, double c) noexcept {
    if (!known(d)) { reject(EINVAL); return; }
    inputs[d] = {q, c};
}
Input input(int d) noexcept {
    if (!known(d)) { reject(EINVAL); return {}; }
    return inputs[d];
}
}

extern "C" int tent_rb_configure(int p, double c0, double c1) noexcept {
    if (p < 0 || p > 5 || !std::isfinite(c0) || !std::isfinite(c1) || c0 <= 0 || c1 <= 0) return -EINVAL;
    if (configured.exchange(true)) return -EALREADY;
    try { records = std::make_unique<tent_rb::Estimate[]>(MaxEstimates); } catch (...) { return -ENOMEM; }
    seeds[0] = c0; seeds[1] = c1; policy.store(p); return 0;
}
extern "C" int tent_rb_policy() noexcept { return policy.load(std::memory_order_relaxed); }
extern "C" int tent_rb_dump(const char* directory) noexcept {
    if (!directory || dumped) return -EINVAL;
    dumped = true;
    try {
        std::string root(directory);
        std::ofstream binary(root + "/rate-estimates.bin", std::ios::binary);
        binary.write(reinterpret_cast<const char*>(records.get()), sizeof(tent_rb::Estimate) * count);
        binary.close();
        std::ofstream meta(root + "/rate-estimates.json");
        meta.precision(17);
        meta << "{\"schema\":\"tent-rb-rate-v1\",\"format\":\"<9Q3d\",\"record_bytes\":96,\"count\":" << count
             << ",\"policy\":" << tent_rb_policy() << ",\"error\":" << error.load()
             << ",\"seed_bps\":[" << seeds[0] << ',' << seeds[1] << "],\"rails\":[";
        bool first = true;
        for (unsigned d = 0; d < Devices; ++d) {
            const auto& c = counters[d]; if (c.rail < 0) continue;
            if (!first) meta << ',';
            first = false;
            meta << "{\"dev\":" << d << ",\"rail\":" << c.rail << ",\"posted\":" << c.posted.load()
                 << ",\"done\":" << c.done.load() << ",\"pending\":" << c.pending.load()
                 << ",\"invalid_completions\":" << c.errors.load() << '}';
        }
        meta << "]}\n"; meta.close();
        return binary && meta ? error.load() ? -error.load() : 0 : -EIO;
    } catch (...) { return -EIO; }
}
