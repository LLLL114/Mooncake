#include "observer.h"

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <thread>
#include <time.h>

namespace tent_obs {
std::atomic<bool> enabled_flag{false};
namespace {
// Budget for the baseline's six workers and up to eight caller threads,
// plus two spare observer threads. These are lifetime thread slots.
constexpr size_t Threads = 16, Contexts = 64, Rows = 131072, Rails = 16;
constexpr size_t RowProbeLimit = 512;
constexpr uint64_t BudgetBytes = 512ULL * 1024 * 1024;
constexpr uint64_t Window = 50000000;
constexpr double Deltas[] = {.001, .01, .05};
enum Kind { Candidate, Weight, WeightTv, AllocationMass, AllocationTv,
            Allocate, ReleaseBw, ReleaseBounds, ReleaseSample, ReleaseSkip,
            ReleaseInterval, Threshold, Post, CqSuccess, CqError, RetryEndpoint,
            RetryPost, RetryCq, PostRejected, CqLateSuccess, CqLateError,
            WorkerQueue, CqQueue, KindCount };
const char* Names[] = {
    "candidate", "weight", "weight_tv", "allocation", "allocation_tv",
    "allocate_call", "release_bw", "release_bounds", "release_sample",
    "release_no_learning", "release_interval", "batch_threshold", "post",
    "cq_success", "cq_error", "retry_endpoint", "retry_post", "retry_cq",
    "post_rejected", "cq_late_success", "cq_late_error", "worker_queue", "cq_queue"};
static_assert(sizeof(Names) / sizeof(*Names) == KindCount, "kind names");
struct Stat {
    double sum, min, max, last;
    void add(double x, uint64_t n) noexcept {
        sum += x;
        if (n == 1) min = max = x;
        else { min = std::min(min, x); max = std::max(max, x); }
        last = x;
    }
};
struct Row {
    uint64_t bin, n, bytes, aux, flags[6];
    uint32_t context;
    int dev, kind;
    Stat value[3];
};
struct Key {
    uint64_t selector, peer, request_bytes, total, slice_bytes, mask, effective,
             candidate_mask;
    uint32_t slices;
    int priority, opcode, worker, site, mode;
    char location[96];
};
struct Context {
    bool used, have_weight, have_allocation;
    Key key;
    double previous_weight[Rails], previous_allocation[Rails];
    double direction[3][Rails];
    uint64_t decisions, comparable_weights, comparable_allocations;
    uint64_t alloc_ns_hist[64];
};
struct Sample { int dev; double score, bandwidth, penalty, weight;
                uint64_t inflight; bool has_weight; };
struct Pending {
    bool active, valid, ok;
    uint64_t stamp;
    Key key;
    double epsilon;
    size_t count;
    Sample samples[Rails];
    const std::vector<int>* result;
};
struct Data {
    Context contexts[Contexts];
    Row rows[Rows];
    Pending pending;
    uint64_t context_overflow, row_overflow, candidate_overflow,
             invalid_values, nested_allocation, clock_errors, long_location,
             missing_allocation, queue_overflow;
    struct Due { bool used; int worker, dev; bool cq; uint64_t bin; } due[128];
    uint64_t release_last_ns[64], release_selector[64];
};
struct alignas(64) Slot { std::atomic<unsigned> busy{0}; Data data{}; };
Slot slots[Threads];
static_assert(sizeof(slots) < BudgetBytes, "observer arena exceeds 512 MiB budget");
static_assert(std::atomic<unsigned>::is_always_lock_free &&
              std::atomic<bool>::is_always_lock_free, "lock-free hooks required");
std::atomic<unsigned> next_slot{0}, rejected_threads{0};
// Only serialized control calls store these; no new global atomic RMW.
std::atomic<unsigned> queue_poll_stride{1};
std::atomic<uint64_t> queue_gate_epoch{0};
uint64_t start_ns = 0, end_ns = 0, epoch = 0;
int control_error = 0;
thread_local int slot_id = -1;
thread_local Origin origin;
thread_local Pending* active_pending = nullptr;

// This scratch belongs exclusively to the calling thread. Control/dump never
// reads or clears it, so skipped polls need no busy handshake. Each key gets
// its own counter: interleaved polling cannot starve a particular NIC.
struct QueuePollGate {
    struct Entry {
        bool used;
        int worker, dev;
        bool cq;
        unsigned phase;
    } entries[128]{};
    uint64_t epoch = 0;
};
bool queue_poll_eligible(int worker, int dev, bool cq) noexcept {
    if (queue_poll_stride.load(std::memory_order_relaxed) == 1) return true;
    thread_local QueuePollGate gate;
    const uint64_t generation = queue_gate_epoch.load(std::memory_order_acquire);
    if (gate.epoch != generation) {
        gate = QueuePollGate{};
        gate.epoch = generation;
    }
    for (auto& e : gate.entries) {
        if (!e.used) {
            e = QueuePollGate::Entry{true, worker, dev, cq, 1};
            return true; // First encounter is eligible, then every 256 calls.
        }
        if (e.worker == worker && e.dev == dev && e.cq == cq) {
            const bool eligible = e.phase == 0;
            e.phase = (e.phase + 1) & 255U;
            return eligible;
        }
    }
    // A full gate must not silently suppress a new key. Fall through to the
    // original guarded sampler, whose existing queue_overflow is disclosed.
    return true;
}

uint64_t clock_ns() noexcept {
    timespec ts{};
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return uint64_t(ts.tv_sec) * 1000000000ULL + uint64_t(ts.tv_nsec);
}
Slot* local_slot() noexcept {
    if (slot_id == -1) {
        unsigned id = next_slot.fetch_add(1, std::memory_order_relaxed);
        slot_id = id < Threads ? int(id) : -2;
        if (slot_id == -2) rejected_threads.fetch_add(1, std::memory_order_relaxed);
    }
    return slot_id >= 0 ? &slots[slot_id] : nullptr;
}
// Seq-cst handshake: a delayed entrant must see disable before touching data.
// busy is private to its owner thread; no steady-state global atomic RMW.
Slot* enter(uint64_t& now) noexcept {
    if (!enabled_flag.load(std::memory_order_seq_cst)) return nullptr;
    Slot* s = local_slot();
    if (!s) return nullptr;
    s->busy.fetch_add(1, std::memory_order_seq_cst);
    if (enabled_flag.load(std::memory_order_seq_cst)) {
        now = clock_ns();
        if (!now) ++s->data.clock_errors;
        if (now && now >= start_ns && now < end_ns) return s;
    }
    s->busy.fetch_sub(1, std::memory_order_seq_cst);
    return nullptr;
}
struct Guard {
    uint64_t now = 0;
    Slot* slot = enter(now);
    ~Guard() { if (slot) slot->busy.fetch_sub(1, std::memory_order_seq_cst); }
};
void disable() noexcept {
    enabled_flag.store(false, std::memory_order_seq_cst);
    for (auto& s : slots)
        while (s.busy.load(std::memory_order_seq_cst)) std::this_thread::yield();
}
uint64_t hash_bytes(const void* ptr, size_t n) noexcept {
    auto p = static_cast<const unsigned char*>(ptr);
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; ++i) h = (h ^ p[i]) * 1099511628211ULL;
    return h;
}
Key base_key() noexcept {
    Key k;
    std::memset(&k, 0, sizeof(k)); // padding participates in exact key comparison
    k.peer = origin.peer; k.request_bytes = origin.request_bytes;
    k.opcode = origin.opcode; k.worker = origin.worker; k.site = origin.site;
    k.mode = 4;
    return k;
}
int context(Data& d, const Key& key) noexcept {
    size_t first = hash_bytes(&key, sizeof(key)) % Contexts;
    for (size_t i = 0; i < Contexts; ++i) {
        auto& c = d.contexts[(first + i) % Contexts];
        if (!c.used) { c.used = true; c.key = key; return int((first + i) % Contexts); }
        if (!std::memcmp(&c.key, &key, sizeof(key))) return int((first + i) % Contexts);
    }
    ++d.context_overflow;
    return -1;
}
Row* row(Data& d, int ctx, uint64_t now, int dev, int kind) noexcept {
    if (ctx < 0) return nullptr;
    uint64_t bin = (now - start_ns) / Window;
    uint64_t h = bin * 11400714819323198485ULL ^ uint64_t(ctx + 1) * 1315423911ULL
                 ^ uint64_t(dev + 2) * 2654435761ULL ^ uint64_t(kind + 1) * 97;
    for (size_t i = 0; i < RowProbeLimit; ++i) {
        auto& r = d.rows[(h + i) % Rows];
        if (!r.n) {
            r.bin = bin; r.context = uint32_t(ctx); r.dev = dev; r.kind = kind;
            return &r;
        }
        if (r.bin == bin && r.context == uint32_t(ctx) && r.dev == dev && r.kind == kind)
            return &r;
    }
    ++d.row_overflow;
    return nullptr;
}
Row* add(Data& d, int ctx, uint64_t now, int dev, int kind,
         uint64_t bytes = 0, uint64_t aux = 0,
         double a = 0, double b = 0, double c = 0) noexcept {
    if (!std::isfinite(a) || !std::isfinite(b) || !std::isfinite(c)) {
        ++d.invalid_values; return nullptr;
    }
    Row* r = row(d, ctx, now, dev, kind);
    if (r) {
        ++r->n; r->bytes += bytes; r->aux += aux;
        r->value[0].add(a, r->n); r->value[1].add(b, r->n); r->value[2].add(c, r->n);
    }
    return r;
}
Pending* pending() noexcept {
    return active_pending;
}
void tv(Data& d, int ctx, Pending& p, const double* values, bool allocation) noexcept {
    Context& c = d.contexts[ctx];
    bool& have = allocation ? c.have_allocation : c.have_weight;
    double* prev = allocation ? c.previous_allocation : c.previous_weight;
    if (have) {
        double step = 0, delta[Rails]{};
        for (size_t i = 0; i < p.count; ++i) {
            delta[i] = values[i] - prev[i]; step += std::abs(delta[i]) * .5;
        }
        if (allocation) ++c.comparable_allocations; else ++c.comparable_weights;
        Row* r = add(d, ctx, p.stamp, -1, allocation ? AllocationTv : WeightTv, 0, 0, step);
        if (!allocation) for (size_t j = 0; j < 3; ++j) {
            if (step <= Deltas[j]) continue;
            double dot = 0;
            for (size_t i = 0; i < p.count; ++i) dot += delta[i] * c.direction[j][i];
            if (r) { ++r->flags[j]; if (dot < 0) ++r->flags[j + 3]; }
            for (size_t i = 0; i < p.count; ++i) c.direction[j][i] = delta[i];
        }
    }
    std::copy(values, values + p.count, prev); have = true;
}
unsigned logbin(uint64_t x) noexcept {
    unsigned b = 0;
    while (x > 1 && b < 63) { x >>= 1; ++b; }
    return b;
}
} // namespace

ContextScope::ContextScope(uint64_t peer, uint64_t bytes, int opcode,
                           int worker, int site) noexcept : on_(enabled()) {
    if (on_) { previous_ = origin; origin = Origin{peer, bytes, opcode, worker, site}; }
}
ContextScope::~ContextScope() { if (on_) origin = previous_; }

Allocation::Allocation(const void* selector, uint64_t total, uint32_t slices,
                       uint64_t slice_bytes, const std::string& location,
                       int priority, uint64_t mask, double epsilon, bool smart,
                       const std::vector<int>& result) noexcept {
    uint64_t now = 0;
    Slot* s = enter(now);
    if (!s) return;
    if (s->data.pending.active) {
        ++s->data.nested_allocation;
        s->data.pending.valid = false;
        s->busy.fetch_sub(1, std::memory_order_seq_cst); return;
    }
    slot_ = s;
    auto& p = s->data.pending;
    p = Pending{}; p.active = p.valid = true; p.stamp = now;
    active_pending = &p;
    p.key = base_key(); p.key.selector = reinterpret_cast<uintptr_t>(selector);
    p.key.total = total; p.key.slices = slices; p.key.slice_bytes = slice_bytes;
    p.key.mask = p.key.effective = mask; p.key.priority = priority;
    p.key.mode = !smart ? 3 : slices == 1 ? 0 : 1;
    if (location.size() >= sizeof(p.key.location)) {
        ++s->data.long_location; p.valid = false;
    } else std::memcpy(p.key.location, location.c_str(), location.size() + 1);
    p.epsilon = epsilon; p.result = &result;
}
Allocation::~Allocation() {
    if (!slot_) return;
    Slot* s = static_cast<Slot*>(slot_);
    Data& d = s->data;
    Pending& p = d.pending;
    if (p.valid) {
        if (p.key.mode == 3) for (int dev : *p.result) {
            bool found = false;
            for (size_t i = 0; i < p.count; ++i) found |= p.samples[i].dev == dev;
            if (!found) {
                if (dev < 0 || dev >= 64 || p.count == Rails) {
                    ++d.candidate_overflow; p.valid = false; break;
                }
                p.samples[p.count++].dev = dev;
                p.key.candidate_mask |= uint64_t(1) << dev;
            }
        }
        // Sort observer scratch only, never the actual selection vector.
        std::sort(p.samples, p.samples + p.count,
                  [](const Sample& a, const Sample& b) { return a.dev < b.dev; });
        int ctx = p.valid ? context(d, p.key) : -1;
        if (ctx >= 0) {
            auto& c = d.contexts[ctx]; ++c.decisions;
            uint64_t finish = clock_ns();
            uint64_t elapsed = finish >= p.stamp ? finish - p.stamp : 0;
            if (!finish) ++d.clock_errors;
            ++c.alloc_ns_hist[logbin(elapsed)];
            auto* call_row = add(d, ctx, p.stamp, -1, Allocate,
                p.key.total, p.ok ? 1 : 0, double(elapsed),
                double(p.key.slices), double(p.result->size()));
            if (call_row) call_row->flags[0] += p.key.total == 0;
            double weights[Rails]{}, mass[Rails]{};
            double total_weight = 0;
            bool weights_valid = p.count && p.key.mode != 3;
            for (size_t i = 0; i < p.count; ++i) {
                auto& a = p.samples[i];
                if (p.key.mode != 3)
                    add(d, ctx, p.stamp, a.dev, Candidate, a.inflight, 0,
                        a.score, a.bandwidth, a.penalty);
                if (p.key.mode == 1) {
                    weights[i] = a.weight; weights_valid &= a.has_weight;
                } else if (p.key.mode != 3) {
                    // Diagnostic transform of captured score, NOT a second
                    // algorithm score or a single/probe selection probability.
                    double denom = a.score + p.epsilon;
                    if (denom <= 0 || !std::isfinite(denom)) weights_valid = false;
                    else { weights[i] = 1.0 / denom; total_weight += weights[i]; }
                }
            }
            if (p.key.mode != 1 && weights_valid) {
                if (total_weight <= 0 || !std::isfinite(total_weight)) weights_valid = false;
                else for (size_t i = 0; i < p.count; ++i) weights[i] /= total_weight;
            }
            for (size_t i = 0; i < p.count; ++i)
                weights_valid &= std::isfinite(weights[i]) && weights[i] >= 0;
            if (weights_valid) {
                for (size_t i = 0; i < p.count; ++i)
                    add(d, ctx, p.stamp, p.samples[i].dev, Weight, 0, 0, weights[i]);
                if (p.key.total) tv(d, ctx, p, weights, false);
            } else if (p.count && p.key.mode != 3) {
                ++d.invalid_values; c.have_weight = false;
                std::memset(c.direction, 0, sizeof(c.direction));
            }
            uint64_t offset = 0;
            uint64_t charged = p.key.slices ? p.key.total / p.key.slices +
                                              (p.key.total % p.key.slices != 0) : 0;
            for (int dev : *p.result) {
                uint64_t bytes = std::min(p.key.slice_bytes, p.key.total - offset);
                offset += bytes;
                uint64_t charge = p.key.mode == 3 ? 0 : p.key.slices == 1 ? p.key.total : charged;
                auto* slice_row = add(d, ctx, p.stamp, dev, AllocationMass,
                                      bytes, charge);
                if (slice_row) slice_row->flags[0] += bytes == 0;
                for (size_t i = 0; i < p.count; ++i)
                    if (p.samples[i].dev == dev) mass[i] += double(bytes);
            }
            if (p.ok && offset == p.key.total && p.count &&
                (p.key.total || p.result->size() == p.key.slices)) {
                if (p.key.total) {
                    for (size_t i = 0; i < p.count; ++i) mass[i] /= double(p.key.total);
                    tv(d, ctx, p, mass, true);
                } else {
                    // The original slicer can emit real zero-length work.
                    // Preserve descriptor counts; a zero byte denominator
                    // has no allocation share or comparable TV sample.
                    c.have_allocation = false;
                }
            } else { ++d.missing_allocation; c.have_allocation = false; }
        }
    }
    active_pending = nullptr;
    p.active = false;
    s->busy.fetch_sub(1, std::memory_order_seq_cst);
}

bool allocation_active() noexcept { return pending() != nullptr; }
void candidate(int dev, double score, uint64_t inflight, double bw,
               double penalty, uint64_t mask) noexcept {
    Pending* p = pending();
    if (!p || !p->valid) return;
    p->key.effective = mask;
    if (dev < 0 || dev >= 64 || p->count == Rails ||
        (p->key.candidate_mask & (uint64_t(1) << dev))) {
        ++slots[slot_id].data.candidate_overflow; p->valid = false; return;
    }
    p->key.candidate_mask |= uint64_t(1) << dev;
    p->samples[p->count++] = Sample{dev, score, bw, penalty, 0, inflight, false};
}
void weight(int dev, double inverse_score, double total_weight) noexcept {
    Pending* p = pending();
    if (!p || !p->valid) return;
    for (size_t i = 0; i < p->count; ++i) if (p->samples[i].dev == dev) {
        p->samples[i].weight = inverse_score / total_weight;
        p->samples[i].has_weight = true; return;
    }
}
void probe(bool yes) noexcept { if (auto* p = pending()) p->key.mode = yes ? 2 : 1; }
void allocation_ok() noexcept { if (auto* p = pending()) p->ok = true; }

void bandwidth_release(const void* selector, int dev, uint64_t length,
                       double latency, bool updated, double old_bw,
                       double observed_bw, double new_bw, double alpha,
                       double theoretical_bw, double min_bw, double max_bw,
                       double unclamped_bw) noexcept {
    Guard g; if (!g.slot) return;
    auto& d = g.slot->data;
    Key k = base_key(); k.selector = reinterpret_cast<uintptr_t>(selector);
    int ctx = context(d, k);
    if (!updated) { add(d, ctx, g.now, dev, ReleaseSkip, length, 0, latency); return; }
    auto* r = add(d, ctx, g.now, dev, ReleaseBw, length, 0, old_bw, observed_bw, new_bw);
    if (r) {
        r->flags[0] += new_bw == min_bw; r->flags[1] += new_bw == max_bw;
        r->flags[2] += unclamped_bw < min_bw; r->flags[3] += unclamped_bw > max_bw;
    }
    add(d, ctx, g.now, dev, ReleaseBounds, 0, 0, theoretical_bw, min_bw, max_bw);
    add(d, ctx, g.now, dev, ReleaseSample, 0, 0, latency, alpha, unclamped_bw);
    if (dev >= 0 && dev < 64) {
        if (d.release_last_ns[dev] && d.release_selector[dev] == k.selector)
            add(d, ctx, g.now, dev, ReleaseInterval, 0, 0,
                double(g.now - d.release_last_ns[dev]));
        d.release_last_ns[dev] = g.now; d.release_selector[dev] = k.selector;
    }
}
void threshold(uint64_t peer, uint64_t bytes, int opcode, uint64_t slices,
               uint64_t threshold_slices, uint64_t block_bytes) noexcept {
    Guard g; if (!g.slot) return;
    Key k = base_key(); k.peer = peer; k.request_bytes = bytes; k.opcode = opcode;
    int ctx = context(g.slot->data, k);
    add(g.slot->data, ctx, g.now, -1, Threshold, bytes, slices >= threshold_slices,
        double(slices), double(threshold_slices), double(block_bytes));
}
void io(Io kind, int worker, int dev, uint64_t bytes, uint64_t retry,
        uint64_t status) noexcept {
    Guard g; if (!g.slot) return;
    Key k = base_key();
    k.peer = k.request_bytes = 0; k.worker = worker; k.site = 2; k.opcode = -1;
    auto& d = g.slot->data;
    add(d, context(d, k), g.now, dev, Post + int(kind), bytes, retry > 0,
        double(retry), double(status));
}
bool queue_due(int worker, int dev, bool cq) noexcept {
    if (!enabled()) return false;
    if (!queue_poll_eligible(worker, dev, cq)) return false;
    Guard g; if (!g.slot) return false;
    auto& d = g.slot->data;
    uint64_t bin = (g.now - start_ns) / Window;
    for (auto& q : d.due) {
        if (!q.used) { q = Data::Due{true, worker, dev, cq, bin}; return true; }
        if (q.worker == worker && q.dev == dev && q.cq == cq) {
            if (q.bin == bin) return false;
            q.bin = bin; return true;
        }
    }
    ++d.queue_overflow; return false;
}
void queue(int worker, int dev, bool cq, uint64_t first, uint64_t second,
           uint64_t third) noexcept {
    Guard g; if (!g.slot) return;
    Key k = base_key();
    k.peer = k.request_bytes = 0; k.worker = worker; k.site = 2; k.opcode = -1;
    auto& d = g.slot->data;
    add(d, context(d, k), g.now, dev, cq ? CqQueue : WorkerQueue, 0, 0,
        double(first), double(second), double(third));
}

namespace {
void quoted(std::ostream& o, const char* text) {
    o << '"';
    for (const unsigned char* p = reinterpret_cast<const unsigned char*>(text); *p; ++p) {
        if (*p == '"' || *p == '\\') o << '\\' << char(*p);
        else if (*p < 32 || *p >= 127) {
            const char* h = "0123456789abcdef";
            o << "\\u00" << h[*p >> 4] << h[*p & 15];
        } else o << char(*p);
    }
    o << '"';
}
void number(std::ostream& o, double x) { if (std::isfinite(x)) o << x; else o << "null"; }
void write_stat(std::ostream& o, const Stat& s, uint64_t n) {
    o << "{\"sum\":"; number(o, s.sum); o << ",\"mean\":"; number(o, s.sum / double(n));
    o << ",\"min\":"; number(o, s.min); o << ",\"max\":"; number(o, s.max);
    o << ",\"last\":"; number(o, s.last); o << '}';
}
struct QueueCoverage {
    uint32_t context;
    int dev, kind;
    uint64_t sampled_bins, samples;
};
void write_json(std::ostream& o) {
    bool incomplete = rejected_threads.load(std::memory_order_relaxed) != 0;
    for (const auto& s : slots) {
        const auto& d = s.data;
        incomplete = incomplete || d.context_overflow || d.row_overflow ||
            d.candidate_overflow || d.invalid_values || d.nested_allocation ||
            d.clock_errors || d.long_location || d.missing_allocation ||
            d.queue_overflow;
    }
    o << std::setprecision(17);
    o << "{\"schema\":\"tent-observer-v1\",\"epoch\":" << epoch
      << ",\"incomplete\":" << (incomplete ? "true" : "false")
      << ",\"clock\":\"CLOCK_MONOTONIC\",\"start_ns\":" << start_ns
      << ",\"end_ns\":" << end_ns << ",\"window_ns\":" << Window
      << ",\"arena_bytes\":" << sizeof(slots)
      << ",\"budget_bytes\":" << BudgetBytes
      << ",\"queue_sampling\":{\"poll_stride\":" << queue_poll_stride.load()
      << ",\"first_poll_eligible\":true,\"key\":\"thread,worker,dev,cq\","
         "\"configured_bins\":"
      << ((end_ns - start_ns) / Window + ((end_ns - start_ns) % Window != 0))
      << ",\"missing_bin_semantics\":\"unobserved, not zero queue\","
         "\"coverage_basis\":\"retained distinct queue rows per bin; not time-weighted\","
         "\"gate_capacity_fallback\":\"use original guarded sampler\","
         "\"non_queue_sampling\":\"unchanged, full-rate\"}"
      << ",\"limits\":{\"threads_lifetime\":" << Threads << ",\"contexts_per_thread\":" << Contexts
      << ",\"rows_per_thread\":" << Rows << ",\"rails_per_decision\":" << Rails
      << ",\"row_probe_limit\":" << RowProbeLimit
      << "},\"rejected_threads_lifetime\":" << rejected_threads.load()
      << ",\"deltas\":[0.001,0.01,0.05],\"threads\":[";
    bool first_thread = true;
    unsigned count = std::min<unsigned>(next_slot.load(), Threads);
    for (unsigned t = 0; t < count; ++t) {
        auto& d = slots[t].data;
        if (!first_thread) o << ',';
        first_thread = false;
        o << "{\"thread_slot\":" << t << ",\"drops\":{\"context\":" << d.context_overflow
          << ",\"row\":" << d.row_overflow << ",\"candidate\":" << d.candidate_overflow
          << ",\"invalid_value\":" << d.invalid_values << ",\"nested_allocation\":" << d.nested_allocation
          << ",\"clock\":" << d.clock_errors << ",\"long_location\":" << d.long_location
          << ",\"missing_allocation\":" << d.missing_allocation
          << ",\"queue\":" << d.queue_overflow << "},\"contexts\":[";
        bool first = true;
        for (size_t i = 0; i < Contexts; ++i) {
            auto& c = d.contexts[i]; if (!c.used) continue;
            auto& k = c.key;
            if (!first) o << ',';
            first = false;
            o << "{\"id\":" << i << ",\"selector\":" << k.selector << ",\"peer\":" << k.peer
              << ",\"request_bytes\":" << k.request_bytes << ",\"total_bytes\":" << k.total
              << ",\"slice_bytes\":" << k.slice_bytes << ",\"slices\":" << k.slices
              << ",\"input_mask\":" << k.mask << ",\"effective_mask\":" << k.effective
              << ",\"candidate_mask\":" << k.candidate_mask << ",\"priority\":" << k.priority
              << ",\"opcode\":" << k.opcode << ",\"worker\":" << k.worker << ",\"site\":" << k.site
              << ",\"mode\":" << k.mode << ",\"location\":";
            quoted(o, k.location);
            o << ",\"weight_semantics\":";
            quoted(o, k.mode == 1 ? "consumed_inverse_score" : k.mode < 3 ? "diagnostic_inverse_score_only" : "N/A");
            o << ",\"allocation_share_tv_semantics\":";
            quoted(o, k.mode == 4 ? "N/A: non-allocation context" :
                      !k.total ? "N/A: zero total bytes" : "effective_byte_share");
            o << ",\"weight_tv_semantics\":";
            quoted(o, k.mode >= 3 ? "N/A: no scored weights" :
                      !k.total ? "N/A: zero total bytes" : "same_context_weight_tv");
            o << ",\"decisions\":" << c.decisions << ",\"comparable_weights\":" << c.comparable_weights
              << ",\"comparable_allocations\":" << c.comparable_allocations << ",\"allocate_ns_log2_hist\":[";
            for (size_t b = 0; b < 64; ++b) { if (b) o << ','; o << c.alloc_ns_hist[b]; }
            o << "]}";
        }
        o << "],\"rows\":["; first = true;
        QueueCoverage queue_coverage[128]{};
        size_t queue_coverage_count = 0;
        uint64_t queue_coverage_overflow = 0;
        for (auto& r : d.rows) {
            if (!r.n) continue;
            if (r.kind == WorkerQueue || r.kind == CqQueue) {
                size_t q = 0;
                for (; q < queue_coverage_count; ++q) {
                    const auto& coverage = queue_coverage[q];
                    if (coverage.context == r.context && coverage.dev == r.dev &&
                        coverage.kind == r.kind) break;
                }
                if (q == queue_coverage_count && q < 128) {
                    queue_coverage[q] = QueueCoverage{r.context, r.dev, r.kind, 0, 0};
                    ++queue_coverage_count;
                }
                if (q < 128) {
                    ++queue_coverage[q].sampled_bins;
                    queue_coverage[q].samples += r.n;
                } else ++queue_coverage_overflow;
            }
            if (!first) o << ',';
            first = false;
            o << "{\"bin\":" << r.bin << ",\"context\":" << r.context << ",\"dev\":" << r.dev
              << ",\"kind\":"; quoted(o, Names[r.kind]);
            o << ",\"n\":" << r.n << ",\"bytes\":" << r.bytes << ",\"aux\":" << r.aux;
            if (r.kind == Allocate)
                o << ",\"zero_length_allocations\":" << r.flags[0];
            if (r.kind == AllocationMass)
                o << ",\"zero_length_slices\":" << r.flags[0];
            o << ",\"flags\":[";
            for (size_t j = 0; j < 6; ++j) { if (j) o << ','; o << r.flags[j]; }
            o << "],\"values\":[";
            for (size_t j = 0; j < 3; ++j) { if (j) o << ','; write_stat(o, r.value[j], r.n); }
            o << "]}";
        }
        o << "],\"queue_sampling_coverage\":[";
        for (size_t q = 0; q < queue_coverage_count; ++q) {
            if (q) o << ',';
            const auto& coverage = queue_coverage[q];
            o << "{\"context\":" << coverage.context << ",\"dev\":" << coverage.dev
              << ",\"kind\":"; quoted(o, Names[coverage.kind]);
            o << ",\"sampled_bins\":" << coverage.sampled_bins
              << ",\"samples\":" << coverage.samples << '}';
        }
        o << "],\"queue_coverage_summary_overflow\":" << queue_coverage_overflow
          << ",\"queue_coverage_summary_incomplete\":"
          << (queue_coverage_overflow ? "true" : "false") << '}';
    }
    o << "],\"unavailable\":{\"persistent_weight_publication\":\"N/A: no original state\","
         "\"flow_switches\":\"N/A: no stable flow ID\","
         "\"cross_rail_retry_unique_bytes\":\"N/A: no reliable logical attempt identity\","
         "\"hardware_queue_bytes_and_depth\":\"N/A: software counters only\","
         "\"request_goodput_and_latency\":\"N/A: main request driver supplies these\","
         "\"globally_ordered_release_intervals\":\"N/A: TLS observations only\"}}\n";
}
} // namespace
} // namespace tent_obs

extern "C" void tent_obs_configure(int enabled, uint64_t start, uint64_t end) noexcept {
    using namespace tent_obs;
    disable(); control_error = 0;
    if (!enabled) return;
    if (end <= start) { control_error = EINVAL; return; }
    // Optional, deliberately restricted to two modes. Default remains 1.
    // Environment access and validation happen outside all worker hot paths.
    unsigned stride = 1;
    if (const char* value = std::getenv("TENT_OBS_QUEUE_POLL_STRIDE")) {
        if (std::strcmp(value, "256") == 0) stride = 256;
        else if (std::strcmp(value, "1") != 0) {
            control_error = EINVAL; return;
        }
    }
    queue_poll_stride.store(stride, std::memory_order_relaxed);
    for (auto& s : slots) std::memset(&s.data, 0, sizeof(s.data));
    start_ns = start; end_ns = end; ++epoch;
    queue_gate_epoch.store(epoch, std::memory_order_release);
    enabled_flag.store(true, std::memory_order_seq_cst);
}
extern "C" void tent_obs_dump(const char* path) noexcept {
    using namespace tent_obs;
    disable(); control_error = 0;
    if (!path || !*path) { control_error = EINVAL; return; }
    try {
        std::ofstream out(path, std::ios::out | std::ios::trunc);
        if (!out) { control_error = errno ? errno : EIO; return; }
        write_json(out); out.flush();
        if (!out) control_error = EIO;
        out.close(); if (out.fail()) control_error = EIO;
    } catch (...) { control_error = EIO; }
}
extern "C" int tent_obs_last_error() noexcept { return tent_obs::control_error; }
