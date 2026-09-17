#include "osc_trace.h"

#include <atomic>
#include <cerrno>
#include <fstream>
#include <memory>
#include <string>

namespace {
constexpr unsigned Slots = 4;
struct Slot {
    std::unique_ptr<tent_osc::Record[]> records;
    std::atomic<uint32_t> count{0};
    std::atomic<uint64_t> dropped{0};
};
Slot slots[Slots];
std::atomic<unsigned> next_slot{0};
std::atomic<int> error{0};
std::atomic<bool> fixed{false}, configured{false}, dumped{false};
std::atomic<double> bandwidth{0};
uint32_t limit = 0;
static_assert(std::atomic<double>::is_always_lock_free);
static_assert(std::atomic<bool>::is_always_lock_free);
}

namespace tent_osc {
void reject(int value) noexcept {
    int expected = 0;
    error.compare_exchange_strong(expected, value, std::memory_order_relaxed);
}
void append(Record record) noexcept {
    static thread_local unsigned owner = next_slot.fetch_add(1, std::memory_order_relaxed);
    if (owner >= Slots || !configured.load(std::memory_order_acquire)) {
        reject(EOVERFLOW);
        return;
    }
    auto& slot = slots[owner];
    const auto index = slot.count.load(std::memory_order_relaxed);
    if (index >= limit) {
        slot.dropped.fetch_add(1, std::memory_order_relaxed);
        reject(EOVERFLOW);
        return;
    }
    record.sequence = index;
    slot.records[index] = record;
    slot.count.store(index + 1, std::memory_order_release);
}
}

extern "C" int tent_osc_configure(int equal, double gbps, uint32_t capacity) noexcept {
    if ((equal != 0 && equal != 1) || (gbps != 0 && gbps != 10) ||
        capacity == 0 || capacity > 200000) return -EINVAL;
    // Only called before engine creation, once in a fresh process.
    if (configured.load() || next_slot.load()) return -EALREADY;
    try {
        for (auto& slot : slots) slot.records = std::make_unique<tent_osc::Record[]>(capacity);
    } catch (...) { return -ENOMEM; }
    limit = capacity;
    fixed.store(equal != 0, std::memory_order_relaxed);
    bandwidth.store(gbps * 1e9 / 8, std::memory_order_relaxed);
    configured.store(true, std::memory_order_release);
    return 0;
}
extern "C" bool tent_osc_fixed() noexcept { return fixed.load(std::memory_order_relaxed); }
extern "C" double tent_osc_bandwidth() noexcept { return bandwidth.load(std::memory_order_relaxed); }
extern "C" int tent_osc_error() noexcept { return error.load(std::memory_order_relaxed); }

extern "C" int tent_osc_dump(const char* directory) noexcept {
    if (!directory || !*directory || !configured.load()) return -EINVAL;
    if (dumped.exchange(true)) return -EALREADY;
    try {
        const std::string root(directory);
        std::ofstream meta(root + "/oscillation-trace.json");
        if (!meta) return -EIO;
        meta << "{\"schema\":\"tent-oscillation-trace-v1\",\"record_bytes\":152,"
             << "\"encoding\":\"little-endian x86_64; <4Q4I2i4Q8d\","
             << "\"fixed_equal\":" << (tent_osc_fixed() ? "true" : "false")
             << ",\"theoretical_override_gbps\":" << tent_osc_bandwidth() * 8e-9
             << ",\"capacity_per_thread\":" << limit
             << ",\"allocated_bytes\":" << uint64_t(Slots) * limit * sizeof(tent_osc::Record)
             << ",\"error\":" << tent_osc_error() << ",\"owners\":" << next_slot.load()
             << ",\"slots\":[";
        const unsigned owners = next_slot.load();
        for (unsigned i = 0; i < owners && i < Slots; ++i) {
            auto n = slots[i].count.load(std::memory_order_acquire);
            std::string name = "oscillation-trace-" + std::to_string(i) + ".bin";
            std::ofstream out(root + "/" + name, std::ios::binary);
            out.write(reinterpret_cast<const char*>(slots[i].records.get()),
                      uint64_t(n) * sizeof(tent_osc::Record));
            out.close();
            if (!out) return -EIO;
            if (i) meta << ',';
            meta << "{\"owner\":" << i << ",\"count\":" << n
                 << ",\"dropped\":" << slots[i].dropped.load()
                 << ",\"file\":\"" << name << "\"}";
        }
        meta << "]}\n";
        meta.close();
        return meta ? 0 : -EIO;
    } catch (...) { return -EIO; }
}
