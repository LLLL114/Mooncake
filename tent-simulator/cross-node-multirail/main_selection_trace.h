#pragma once
#include "osc_trace.h"
#include <atomic>
#include <cstdint>
#include <vector>

namespace main_trace {
extern std::atomic<bool> enabled;
struct RequestScope {
    uint32_t previous=0;
    RequestScope(const void* source,uint64_t length) noexcept;
    ~RequestScope();
};
struct Allocation {
    tent_osc::Record record{};
    const std::vector<int>& result;
    uint64_t block;
    double epsilon;
    unsigned candidates=0;
    bool active=false,ok=false;
    Allocation(const void* selector,uint64_t total,uint32_t slices,uint64_t block,
               double epsilon,const std::vector<int>& result) noexcept;
    ~Allocation();
};
void candidate(int dev,double score,uint64_t inflight,double bandwidth,double penalty) noexcept;
void weight(int dev,double inverse,double total) noexcept;
void probe(bool value) noexcept;
void success() noexcept;
}

extern "C" __attribute__((visibility("default"))) void tent_obs_configure(int enabled,uint64_t start,uint64_t end) noexcept;
extern "C" __attribute__((visibility("default"))) void tent_obs_dump(const char* path) noexcept;
extern "C" __attribute__((visibility("default"))) int tent_obs_last_error() noexcept;
