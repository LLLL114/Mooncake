#pragma once
#include <array>
#include <cstdint>
#include <mutex>
#include <unordered_map>
#include <vector>

namespace mooncake::tent::multirail {
enum class Policy { LargestRemainder, EarliestFinish, ByteDeficit };
struct Candidate {
    int id;
    double score;                    // legacy score in seconds; only LargestRemainder uses it
    double nominal_bytes_per_second;
    double numa_penalty;
};
struct Parameters {
    uint64_t capacity_tau_ns = 20000000;
    uint64_t target_tau_ns = 20000000;
    uint64_t target_interval_ns = 1000000;
    uint64_t sample_min_ns = 20000;
    uint64_t sample_min_bytes = 262144;
    uint64_t sample_burst_span_ns = 10000;
    uint64_t probe_interval_ns = 10000000;
    double probe_fraction = .01;
    double target_hysteresis = .02;
    double max_target_step = .10;
    double congestion_ratio = .25;
    uint64_t congestion_slack_ns = 10000;
    uint64_t affinity_slack_ns = 2000;
    uint32_t qp_limit = 128;
};
struct RailStats {
    uint64_t reserved_bytes=0, posted=0, posted_bytes=0, updates=0, last_sent_ns=0;
    double capacity=0, latency_ns=1000;
};
struct Assignment { std::vector<int> ids; std::vector<uint64_t> bytes; std::vector<double> weights; };
// Stateless integer allocation. Candidate scores must be finite and positive.
Assignment allocateLargestRemainder(const std::vector<Candidate>& candidates,
                                    const std::vector<uint64_t>& slices,
                                    bool round_robin_probe=false);
// Each instance represents one coordinated reservation domain. No waits for QP
// credits occur inside allocate; the caller queues/posts the returned slices.
class Scheduler {
 public:
    explicit Scheduler(Policy policy, Parameters parameters={});
    Assignment allocate(const std::vector<Candidate>& candidates,
                        const std::vector<uint64_t>& slices, uint64_t now_ns,
                        uint64_t traffic_class=0, bool legacy_probe=false);
    void posted(int id, uint64_t bytes, uint64_t post_ns);
    void completed(int id, uint64_t bytes, uint64_t post_ns, uint64_t now_ns,
                   bool was_posted, bool success);
    std::array<RailStats,64> stats() const;
 private:
    struct Rail {
        RailStats stats;
        double nominal=0;
        uint64_t first_post=0, window_start=0, window_bytes=0, last_update=0, last_event=0, window_post=0;
        bool first_pending=false, window_started=false, latency_initialized=false;
    };
    struct Targets {
        std::array<double,64> weight{}, credit{};
        uint64_t mask=0, last_update=0;
    };
    Assignment allocateEarliestFinish(const std::vector<Candidate>&,
                                     const std::vector<uint64_t>&, uint64_t);
    Assignment allocateByteDeficit(const std::vector<Candidate>&,
                                  const std::vector<uint64_t>&, uint64_t, uint64_t);
    double finishCost(const Candidate&,uint64_t) const;
    int probeCandidate(const std::vector<Candidate>&,uint64_t,uint64_t) const;
    void reserve(int,uint64_t,uint64_t);
    Policy policy_;
    Parameters parameters_;
    mutable std::mutex mutex_;
    std::array<Rail,64> rails_{};
    std::unordered_map<uint64_t,Targets> targets_;
    double probe_credit_=0;
};
} // namespace mooncake::tent::multirail
