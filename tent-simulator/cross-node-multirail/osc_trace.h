#pragma once
#include <cstdint>

namespace tent_osc {
struct Record {
    uint64_t ns, sequence, total_bytes, selector;
    uint32_t slices, mode, context, site;
    int32_t dev[2];
    uint64_t inflight[2], assigned[2];
    double score[2], bandwidth[2], penalty[2], weight[2];
};
static_assert(sizeof(Record) == 152);
void append(Record record) noexcept;
void reject(int error) noexcept;
}
extern "C" int tent_osc_configure(int fixed_equal, double theoretical_gbps,
                                   uint32_t capacity) noexcept;
extern "C" bool tent_osc_fixed() noexcept;
extern "C" double tent_osc_bandwidth() noexcept;
extern "C" int tent_osc_error() noexcept;
extern "C" int tent_osc_dump(const char* directory) noexcept;
