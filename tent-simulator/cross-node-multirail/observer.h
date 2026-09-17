#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

// Control calls are serialized by the driver. Disable drains active hooks.
// enabled=1 starts a fresh epoch; [start_ns,end_ns) uses CLOCK_MONOTONIC.
#define TENT_OBS_API __attribute__((visibility("default")))
extern "C" {
TENT_OBS_API void tent_obs_configure(int enabled, uint64_t start_ns, uint64_t end_ns) noexcept;
TENT_OBS_API void tent_obs_dump(const char* path) noexcept;
TENT_OBS_API int tent_obs_last_error() noexcept;  // errno value; 0 means success
}

namespace tent_obs {
extern std::atomic<bool> enabled_flag;
inline bool enabled() noexcept {
    return enabled_flag.load(std::memory_order_relaxed);
}

struct Origin {
    uint64_t peer = 0, request_bytes = 0;
    int opcode = -1, worker = -1, site = 0; // 0 direct, 1 transport, 2 worker
};
class ContextScope {
 public:
    ContextScope(uint64_t peer, uint64_t request_bytes, int opcode,
                 int worker, int site) noexcept;
    ~ContextScope();
    ContextScope(const ContextScope&) = delete;
    ContextScope& operator=(const ContextScope&) = delete;
 private:
    Origin previous_;
    bool on_;
};

// The scope spans the original allocate body, including every early return.
class Allocation {
 public:
    Allocation(const void* selector, uint64_t total, uint32_t slices,
               uint64_t slice_bytes, const std::string& location, int priority,
               uint64_t mask, double epsilon, bool smart,
               const std::vector<int>& result) noexcept;
    ~Allocation();
    Allocation(const Allocation&) = delete;
    Allocation& operator=(const Allocation&) = delete;
 private:
    void* slot_ = nullptr;
};
bool allocation_active() noexcept;
void candidate(int dev, double score, uint64_t inflight, double bandwidth,
               double rank_penalty, uint64_t effective_mask) noexcept;
void weight(int dev, double inverse_score, double total_weight) noexcept;
void probe(bool is_probe) noexcept;
void allocation_ok() noexcept;

void bandwidth_release(const void* selector, int dev, uint64_t length,
                       double latency, bool updated, double old_bw,
                       double observed_bw, double new_bw, double alpha,
                       double theoretical_bw, double min_bw, double max_bw,
                       double unclamped_bw) noexcept;
void threshold(uint64_t peer, uint64_t bytes, int opcode, uint64_t slices,
               uint64_t threshold_slices, uint64_t block_bytes) noexcept;
enum class Io : int { Post, CqSuccess, CqError, RetryEndpoint, RetryPost,
                      RetryCq, PostRejected, CqLateSuccess, CqLateError };
void io(Io kind, int worker, int dev, uint64_t bytes, uint64_t retry,
        uint64_t status = 0) noexcept;
// Sampled at most once per (thread,worker,dev,kind,50ms), before reading fields.
bool queue_due(int worker, int dev, bool cq) noexcept;
void queue(int worker, int dev, bool cq, uint64_t first, uint64_t second,
           uint64_t third) noexcept;
}  // namespace tent_obs

#define TENT_OBS(...) do { if (::tent_obs::enabled()) { __VA_ARGS__; } } while (0)

#define TENT_OBS_ALLOC(...) do { if (::tent_obs::allocation_active()) { __VA_ARGS__; } } while (0)
