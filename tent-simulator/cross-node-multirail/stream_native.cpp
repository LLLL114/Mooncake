// C++17. Link against the TENT C API; Python owns the engine and both buffers.
// Return: 0 success, 1 bounded load stop, -1 configuration/setup error,
// -2 transfer/API/timeout failure (drained), -3 output error (drained),
// -4 NOT DRAINED: retain engine/registrations/buffers and let watchdog kill.
// One engine, callers native threads. window/rate/queue_capacity are TOTALS.
// All timestamps are absolute CLOCK_MONOTONIC nanoseconds, not wall time.
// Output directory must already exist. No filesystem writes occur during input.
#include "tent/transfer_engine.h"
#include "tent/thirdparty/nlohmann/json.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <dlfcn.h>
#include <fstream>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <time.h>
#include <vector>

namespace {
using json = nlohmann::json;
constexpr uint64_t kMaxRecords = 2000000;
constexpr uint64_t kBillion = 1000000000;
constexpr size_t kNoRecord = std::numeric_limits<size_t>::max();

uint64_t now_ns() {
    timespec ts{};
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        throw std::runtime_error("clock_gettime failed");
    return uint64_t(ts.tv_sec) * kBillion + uint64_t(ts.tv_nsec);
}

uint64_t duration_ns(double seconds, bool allow_zero = false) {
    // Bound conversions and additions well below uint64_t overflow.
    if (!std::isfinite(seconds) || seconds < 0 || seconds > 86400 ||
        (!allow_zero && seconds < 1e-9))
        throw std::invalid_argument("durations must be in [1ns,86400s]");
    return static_cast<uint64_t>(std::round(seconds * kBillion));
}

uint64_t positive_integer(const json& value, const char* name) {
    if (!value.is_number_integer() ||
        (!value.is_number_unsigned() && value.get<int64_t>() <= 0) ||
        (value.is_number_unsigned() && value.get<uint64_t>() == 0))
        throw std::invalid_argument(name);
    return value.get<uint64_t>();
}

double rate_value(const json& value) {
    if (!value.is_number()) throw std::invalid_argument("rate must be numeric");
    double rate = value.get<double>();
    if (!std::isfinite(rate) || rate < 0)
        throw std::invalid_argument("rate must be finite and nonnegative");
    return rate;
}

struct Step { uint64_t duration; double rate; };
struct Config {
    uint64_t size, window, callers, seconds, warmup, deadline, lateness, queue_capacity;
    uint64_t observer_prepare, warmup_drain;
    double rate;
    bool observer_enabled;
    std::string alignment;
    std::vector<Step> steps;

    explicit Config(const json& j) {
        if (!j.is_object()) throw std::invalid_argument("config must be an object");
        size = positive_integer(j.at("size"), "size must be a positive integer");
        window = positive_integer(j.at("window"), "window must be a positive integer");
        callers = positive_integer(j.value("callers", json(1)), "invalid callers");
        if (callers > window || callers > 64)
            throw std::invalid_argument("callers must be <=window and <=64");
        alignment = j.value("arrival_alignment", std::string("staggered"));
        if (alignment != "synchronized" && alignment != "staggered")
            throw std::invalid_argument("arrival_alignment must be synchronized or staggered");
        if (size < 16 || window > kMaxRecords)
            throw std::invalid_argument("size must be >=16; window must be <=2000000");
        seconds = duration_ns(j.at("seconds").get<double>());
        warmup = duration_ns(j.value("warmup", 0.0), true);
        deadline = duration_ns(j.value("deadline_seconds", 30.0));
        observer_prepare = duration_ns(j.value("observer_prepare_seconds", 5.0));
        warmup_drain = duration_ns(j.value("warmup_drain_seconds", 1.0));
        lateness = duration_ns(j.value("max_lateness_seconds", 1.0));
        queue_capacity = positive_integer(j.value("queue_capacity", json(window)),
                                          "queue_capacity must be a positive integer");
        if (queue_capacity > kMaxRecords || queue_capacity < callers)
            throw std::invalid_argument("queue_capacity must be >=callers and <=2000000");
        rate = rate_value(j.value("rate_bytes_per_second", json(0)));
        observer_enabled = j.value("observer_enabled", true);
        if (j.contains("steps")) {
            const auto& list = j.at("steps");
            if (!list.is_array() || list.empty() || list.size() > 10000)
                throw std::invalid_argument("steps must have 1..10000 entries");
            uint64_t sum = 0;
            for (const auto& s : list) {
                Step step{duration_ns(s.at("seconds").get<double>()),
                          rate_value(s.at("rate_bytes_per_second"))};
                sum += step.duration;
                steps.push_back(step);
            }
            if (sum != seconds)
                throw std::invalid_argument("step durations (rounded ns) must sum to seconds");
        } else {
            steps.push_back({seconds, rate});
        }
    }
};

struct Record {
    uint64_t planned = 0, submitted = 0, finished = 0, epoch = 0;
    uint64_t transferred = 0;
    size_t slot = kNoRecord;
    uint64_t flow_id = 0;
    int native_status = -1, submit_rc = 0, poll_rc = 0, cancel_rc = 0, free_rc = 0;
    // 0 pending (including cleanup pending), 1 success, 2 failure.
    int result = 0;
    bool measurement = false, cancel_requested = false, queued = false;
    const char* reason = nullptr;
};
struct Slot {
    tent_batch_id_t batch = 0;
    size_t record = kNoRecord;
    tent_request_t request{};
    uint64_t final_epoch = 0;
    bool terminal = false;
};

bool terminal_status(int status) {
    return status == STATUS_COMPLETED || status == STATUS_CANCELED ||
           status == STATUS_TIMEOUT || status == STATUS_FAILED;
    // INVALID is not proof that device access stopped: cancel and keep polling.
}

// First failure stops every producer; hot record/slot/counter state is private.
struct SharedStop {
    std::atomic<uint64_t> at{0};
    std::atomic<bool> failure{false};
};

size_t partition_size(uint64_t total, uint64_t callers, uint64_t flow) {
    return total / callers + (flow < total % callers ? 1 : 0);
}

size_t partition_start(uint64_t total, uint64_t callers, uint64_t flow) {
    return flow * (total / callers) + std::min(flow, total % callers);
}

struct Stream {
    void* engine;
    uint64_t target_id, source_base, target_base, stride;
    Config c;
    SharedStop& shared;
    uint64_t flow_id;
    size_t slot_start, record_capacity;
    std::vector<Record> records;
    std::vector<Slot> slots;
    std::vector<size_t> queue;
    size_t qhead = 0, qcount = 0, next_slot = 0;
    size_t queue_peak = 0;
    uint64_t measurement_start = 0, measurement_end = 0, scheduled_end = 0;
    uint64_t warmup_start = 0, warmup_end = 0, drain_end = 0;
    uint64_t input_stop = 0;
    uint64_t abort_at = 0, first_unaccepted_planned = 0;
    uint64_t rejected = 0, measurement_rejected = 0;
    const char* stop_reason = nullptr;
    bool overflow = false, aborting = false, unsafe = false;
    int return_code = 0;
    Stream(void* e, uint64_t t, uint64_t s, uint64_t d, uint64_t st, Config cfg,
           SharedStop& shared_stop, uint64_t flow)
        : engine(e), target_id(t), source_base(s), target_base(d), stride(st),
          c(std::move(cfg)), shared(shared_stop), flow_id(flow),
          slot_start(partition_start(c.window, c.callers, flow)),
          record_capacity(partition_size(kMaxRecords, c.callers, flow)),
          slots(partition_size(c.window, c.callers, flow)),
          queue(partition_size(c.queue_capacity, c.callers, flow)) {
        records.reserve(record_capacity); // No record allocation in the hot loop.
    }

    void stop(const char* why, uint64_t now, bool load_stop = false) {
        if (!load_stop) shared.failure.store(true);
        uint64_t unset = 0;
        shared.at.compare_exchange_strong(unset, now);
        if (!stop_reason) stop_reason = why;
        if (!aborting) abort_at = shared.at.load();
        aborting = true;
        overflow = overflow || load_stop;
        if (!load_stop || return_code == 0) return_code = load_stop ? 1 : -2;
    }

    void sync_stop() {
        uint64_t at = shared.at.load();
        if (at && !aborting)
            stop("peer_caller_stopped", at, !shared.failure.load());
    }

    void reject(uint64_t planned, bool measurement) {
        first_unaccepted_planned = planned;
        ++rejected;
        if (measurement) ++measurement_rejected;
    }

    size_t outstanding() const {
        size_t count = 0;
        for (const auto& slot : slots) if (slot.batch) ++count;
        return count;
    }

    size_t free_slot() {
        for (size_t i = 0; i < slots.size(); ++i) {
            size_t n = (next_slot + i) % slots.size();
            if (!slots[n].batch) {
                next_slot = (n + 1) % slots.size();
                return n;
            }
        }
        return kNoRecord;
    }

    size_t accept(uint64_t planned, bool measurement) {
        Record r;
        r.planned = planned;
        // Unique, increasing per caller/slot; independent of thread interleaving.
        r.epoch = records.size() * c.callers + flow_id + 1;
        r.flow_id = flow_id;
        r.measurement = measurement;
        records.push_back(r);
        return records.size() - 1;
    }

    void submit(size_t index, size_t slot_index) {
        auto& r = records[index];
        auto& slot = slots[slot_index];
        r.slot = slot_start + slot_index;
        slot.batch = tent_allocate_batch(engine, 1);
        if (!slot.batch) {
            r.result = 2;
            r.finished = now_ns();
            r.reason = "allocate_batch_failed";
            stop(r.reason, r.finished);
            return;
        }
        slot.record = index;
        slot.terminal = false;
        // Failed/canceled later writes may overwrite an earlier successful
        // epoch. Such a slot cannot be used for receiver verification.
        slot.final_epoch = 0;
        slot.request = tent_request_t{};
        auto* payload = reinterpret_cast<unsigned char*>(
            static_cast<uintptr_t>(source_base + r.slot * stride + 64));
        for (unsigned b = 0; b < 8; ++b) {
            payload[b] = static_cast<unsigned char>(r.epoch >> (b * 8));
            payload[c.size - 8 + b] = payload[b];
        }
        slot.request.opcode = OPCODE_WRITE;
        slot.request.source = payload;
        slot.request.target_id = target_id;
        slot.request.target_offset = target_base + r.slot * stride + 64;
        slot.request.length = c.size;
        slot.request.transport_hint = 1;
        r.submitted = now_ns();
        r.submit_rc = tent_submit(engine, slot.batch, &slot.request, 1);
        if (r.submit_rc != 0) {
            // Submission may be partial even when the C wrapper returns error.
            r.reason = "submit_failed";
            stop(r.reason, now_ns());
        }
    }

    void poll() {
        sync_stop();
        for (auto& slot : slots) {
            if (!slot.batch || slot.terminal) continue;
            auto& r = records[slot.record];
            tent_status_t status{};
            int rc = tent_task_status(engine, slot.batch, 0, &status);
            uint64_t now = now_ns();
            if (rc != 0) {
                r.poll_rc = rc;
                if (!r.reason) r.reason = "task_status_api_error";
                stop(r.reason, now);
            } else {
                r.native_status = status.status;
                r.transferred = status.transferred_bytes;
                if (terminal_status(status.status)) {
                    slot.terminal = true;
                    r.finished = now;
                    // Observation past the local deadline is a timeout even if
                    // the engine finally reports COMPLETED. Preserve native_status.
                    if (!r.reason && now - r.submitted >= c.deadline)
                        r.reason = "request_deadline_exceeded";
                    if (!r.reason && status.status != STATUS_COMPLETED) {
                        r.reason = status.status == STATUS_TIMEOUT ? "native_timeout" :
                                   status.status == STATUS_CANCELED ? "native_canceled" :
                                   "native_failed";
                    }
                    if (!r.reason && status.transferred_bytes != c.size)
                        r.reason = "completed_byte_count_mismatch";
                    r.free_rc = tent_free_batch(engine, slot.batch);
                    if (r.free_rc != 0) {
                        r.reason = "free_batch_failed";
                        // Do not retry a possibly destructive free or reuse slot.
                    } else {
                        slot.batch = 0;
                    }
                    r.result = r.free_rc != 0 ? 0 : r.reason ? 2 : 1;
                    if (r.result == 1) slot.final_epoch = r.epoch;
                    else if (r.free_rc != 0 || !r.cancel_requested ||
                             status.status != STATUS_CANCELED)
                        stop(r.reason, now);
                    continue;
                }
                if (status.status != STATUS_WAITING && status.status != STATUS_PENDING) {
                    if (!r.reason) r.reason = "invalid_or_unknown_native_status";
                    stop(r.reason, now);
                }
            }
            if (now - r.submitted >= c.deadline) {
                if (!r.reason) r.reason = "request_deadline_exceeded";
                stop(r.reason, now);
            }
        }
    }

    void cancel() {
        uint64_t now = now_ns();
        while (qcount) {
            auto& r = records[queue[qhead]];
            qhead = (qhead + 1) % queue.size();
            --qcount;
            r.result = 2;
            r.finished = now;
            r.reason = "not_submitted_after_stop";
        }
        for (auto& slot : slots) {
            if (!slot.batch || slot.terminal) continue;
            auto& r = records[slot.record];
            if (r.cancel_requested) continue;
            r.cancel_requested = true;
            r.cancel_rc = tent_cancel_task(engine, slot.batch, 0);
            // A successful cancel is NOT a terminal observation.
        }
    }

    void dispatch() {
        while (qcount && !aborting) {
            sync_stop();
            if (aborting) break;
            auto& r = records[queue[qhead]];
            uint64_t now = now_ns();
            if (now - r.planned > c.lateness) {
                stop("max_lateness_exceeded", now, true);
                break;
            }
            size_t slot = free_slot();
            if (slot == kNoRecord) break;
            size_t index = queue[qhead];
            qhead = (qhead + 1) % queue.size();
            --qcount;
            submit(index, slot);
        }
    }

    void drain(uint64_t input_end) {
        while (qcount || outstanding()) {
            poll();
            uint64_t now = now_ns();
            if (!aborting && now - input_end >= c.deadline)
                stop("drain_deadline_exceeded", now);
            if (aborting) {
                cancel();
                if (now - abort_at >= c.deadline) break;
            } else {
                dispatch();
            }
        }
        drain_end = now_ns();
        unsafe = outstanding() != 0;
        if (unsafe) return_code = -4;
    }

    // Rank determines the fixed total input count; alignment only changes time.
    long double offset_ns(uint64_t sequence, double rate, bool aligned) const {
        long double rank = static_cast<long double>(sequence) * c.callers;
        if (!aligned || c.alignment == "staggered") rank += flow_id;
        return rank * c.size * kBillion / rate;
    }

    void phase(bool measurement, uint64_t start, uint64_t length,
               const std::vector<Step>& steps) {
        uint64_t end = start + length;
        if (measurement) {
            measurement_start = start;
            measurement_end = end;
            scheduled_end = end;
        } else warmup_start = start;
        // The thread must be armed before the fixed boundary. Do not rebase a
        // producer that was released or scheduled too late onto a new start.
        uint64_t armed_at = now_ns();
        if (armed_at >= start) stop("caller_missed_start", armed_at);
        while (now_ns() < start && !shared.at.load()) std::this_thread::yield();
        size_t step_index = 0;
        uint64_t step_start = start, sequence = 0;
        bool input_done = false;
        while (!input_done && !aborting) {
            poll();
            if (aborting) break;
            dispatch();
            // Bounded admission work per poll; the next iteration continues
            // the original sequence, never rebases a late arrival to now.
            for (unsigned work = 0; work < 256 && !aborting; ++work) {
                sync_stop();
                if (aborting) break;
                uint64_t now = now_ns();
                if (now >= end) {
                    // Do not silently omit overdue scheduled arrivals at cutoff.
                    uint64_t remaining_start = step_start;
                    for (size_t n = step_index; n < steps.size(); ++n) {
                        const auto& remaining = steps[n];
                        uint64_t seq = n == step_index ? sequence : 0;
                        if (remaining.rate > 0) {
                            if (offset_ns(seq, remaining.rate, false) < remaining.duration) {
                                reject(remaining_start + static_cast<uint64_t>(
                                           offset_ns(seq, remaining.rate, true)),
                                       measurement);
                                stop("scheduled_arrivals_unaccepted_at_end", now, true);
                                break;
                            }
                        }
                        remaining_start += remaining.duration;
                    }
                    input_done = true;
                    break;
                }
                if (step_index == steps.size()) { input_done = true; break; }
                const auto& step = steps[step_index];
                uint64_t step_end = step_start + step.duration;
                uint64_t planned;
                if (step.rate == 0) {
                    if (now >= step_end) {
                        step_start = step_end;
                        ++step_index;
                        sequence = 0;
                        continue;
                    }
                    if (qcount) break;
                    planned = now;
                } else {
                    if (offset_ns(sequence, step.rate, false) >= step.duration) {
                        // Wait for boundary so closed-loop steps cannot start early.
                        if (now < step_end) break;
                        step_start = step_end;
                        ++step_index;
                        sequence = 0;
                        continue;
                    }
                    planned = step_start + static_cast<uint64_t>(
                        offset_ns(sequence, step.rate, true));
                    if (planned > now) break;
                    if (now - planned > c.lateness) {
                        reject(planned, measurement);
                        stop("max_lateness_exceeded", now, true);
                        break;
                    }
                }
                size_t slot = qcount ? kNoRecord : free_slot();
                if (step.rate == 0 && slot == kNoRecord) break;
                if (records.size() == record_capacity ||
                    (slot == kNoRecord && qcount == queue.size())) {
                    reject(planned, measurement);
                    stop(records.size() == record_capacity ? "record_limit_reached" :
                         "queue_capacity_exceeded", now, true);
                    break;
                }
                size_t index = accept(planned, measurement);
                ++sequence;
                if (slot != kNoRecord) submit(index, slot);
                else {
                    records[index].queued = true;
                    queue[(qhead + qcount) % queue.size()] = index;
                    ++qcount;
                    queue_peak = std::max(queue_peak, qcount);
                }
            }
        }
        uint64_t stopped = std::max(start, std::min(now_ns(), end));
        if (measurement) input_stop = stopped;
        else warmup_end = stopped;
        drain(stopped);
    }

    void emergency_cleanup() noexcept {
        try {
            stop("native_exception", now_ns());
            if (measurement_start && !input_stop)
                input_stop = std::min(abort_at, scheduled_end);
            cancel();
            drain(abort_at);
        } catch (...) {
            unsafe = outstanding() != 0;
            return_code = unsafe ? -4 : -2;
        }
    }

    json counts(int phase) const {
        uint64_t accepted = 0, success = 0, failure = 0, backpressure = 0, submitted = 0;
        for (const auto& r : records) {
            if (phase != -1 && r.measurement != (phase == 1)) continue;
            ++accepted;
            success += r.result == 1;
            failure += r.result == 2;
            backpressure += r.queued;
            submitted += r.submitted != 0;
        }
        return {{"accepted", accepted}, {"success", success}, {"failure", failure},
                {"pending", accepted - success - failure}, {"submitted", submitted},
                {"backpressure", backpressure},
                {"rejected", phase == -1 ? rejected :
                             phase == 1 ? measurement_rejected : rejected - measurement_rejected}};
    }

};

struct Coordinator {
    Config c;
    SharedStop shared;
    std::vector<std::unique_ptr<Stream>> workers;
    std::vector<std::thread> threads;
    std::mutex mutex;
    std::condition_variable changed;
    size_t ready = 0, done = 0;
    uint64_t generation = 0, phase_start = 0;
    bool quit = false, measuring = false;
    uint64_t measurement_start = 0, measurement_end = 0;
    uint64_t warmup_start = 0, warmup_end = 0, drain_end = 0, input_stop = 0;
    uint64_t observer_prepare_started = 0, observer_prepare_finished = 0;
    bool start_missed = false;
    bool unsafe = false, overflow = false, aborting = false;
    const char* stop_reason = nullptr;
    uint64_t first_unaccepted_planned = 0;
    int return_code = 0;
    using ObserverConfigure = void (*)(int, uint64_t, uint64_t);
    ObserverConfigure observer = nullptr;

    Coordinator(void* e, uint64_t t, uint64_t s, uint64_t d, uint64_t st, Config cfg)
        : c(std::move(cfg)) {
        for (uint64_t flow = 0; flow < c.callers; ++flow)
            workers.emplace_back(new Stream(e, t, s, d, st, c, shared, flow));
        threads.reserve(c.callers);
        observer = reinterpret_cast<ObserverConfigure>(
            dlsym(RTLD_DEFAULT, "tent_obs_configure"));
    }

    void worker_main(Stream& worker) {
        std::unique_lock<std::mutex> lock(mutex);
        ++ready;
        changed.notify_all();
        uint64_t seen = 0;
        for (;;) {
            changed.wait(lock, [&] { return quit || generation != seen; });
            if (quit) return;
            seen = generation;
            bool measurement = measuring;
            uint64_t start = phase_start;
            lock.unlock();
            try {
                if (measurement) worker.phase(true, start, c.seconds, c.steps);
                else worker.phase(false, start, c.warmup, {{c.warmup, c.steps.front().rate}});
            } catch (...) {
                worker.emergency_cleanup();
            }
            lock.lock();
            ++done;
            changed.notify_all();
        }
    }

    void missed_start(const char* reason, uint64_t now) {
        start_missed = true;
        stop_reason = reason;
        shared.failure.store(true);
        uint64_t unset = 0;
        shared.at.compare_exchange_strong(unset, now);
    }

    bool prepare_timeline() {
        // Fix the entire experiment before reset: large observer arenas may
        // take time to clear. That work must never fall into warmup/measurement.
        uint64_t first_start = now_ns() + c.observer_prepare;
        if (c.warmup) {
            warmup_start = first_start;
            warmup_end = warmup_start + c.warmup;
            measurement_start = warmup_end + c.warmup_drain;
        } else {
            measurement_start = first_start;
        }
        measurement_end = measurement_start + c.seconds;
        for (auto& worker : workers) {
            worker->measurement_start = measurement_start;
            worker->measurement_end = measurement_end;
            worker->scheduled_end = measurement_end;
        }
        observer_prepare_started = now_ns();
        // Exactly one coordinator callback, before releasing ANY producer.
        // Warmup is excluded by the future measurement boundaries in the hook.
        if (observer) observer(c.observer_enabled ? 1 : 0,
                               measurement_start, measurement_end);
        observer_prepare_finished = now_ns();
        if (observer_prepare_finished >= first_start) {
            missed_start("observer_prepare_missed_start", observer_prepare_finished);
            return false;
        }
        return true;
    }

    void run_phase(bool measurement) {
        std::unique_lock<std::mutex> lock(mutex);
        phase_start = measurement ? measurement_start : warmup_start;
        uint64_t released_at = now_ns();
        if (released_at >= phase_start) {
            missed_start(measurement ? "measurement_start_missed" : "warmup_start_missed",
                         released_at);
            return;
        }
        measuring = measurement;
        done = 0;
        ++generation;
        changed.notify_all();
        changed.wait(lock, [&] { return done == workers.size(); });
    }

    void join() {
        {
            std::lock_guard<std::mutex> lock(mutex);
            quit = true;
        }
        changed.notify_all();
        for (auto& thread : threads) if (thread.joinable()) thread.join();
    }

    void collect() {
        unsafe = false;
        for (const auto& worker : workers) {
            unsafe = unsafe || worker->unsafe || worker->outstanding() != 0;
            overflow = overflow || worker->overflow;
            aborting = aborting || worker->aborting;
            drain_end = std::max(drain_end, worker->drain_end);
            input_stop = std::max(input_stop, worker->input_stop);
            if (worker->stop_reason &&
                std::string(worker->stop_reason) != "peer_caller_stopped" && !stop_reason)
                stop_reason = worker->stop_reason;
            if (worker->stop_reason && std::string(worker->stop_reason) == "caller_missed_start")
                start_missed = true;
            if (worker->first_unaccepted_planned && (!first_unaccepted_planned ||
                worker->first_unaccepted_planned < first_unaccepted_planned))
                first_unaccepted_planned = worker->first_unaccepted_planned;
        }
        return_code = unsafe ? -4 : shared.failure.load() ? -2 : overflow ? 1 : 0;
    }

    void run() {
        try {
            for (auto& worker : workers) {
                Stream* ptr = worker.get();
                threads.emplace_back([this, ptr] { worker_main(*ptr); });
            }
            {
                std::unique_lock<std::mutex> lock(mutex);
                changed.wait(lock, [&] { return ready == workers.size(); });
            }
            if (prepare_timeline()) {
                if (c.warmup) run_phase(false);
                if (!shared.at.load()) run_phase(true);
            }
        } catch (...) {
            shared.failure.store(true);
            uint64_t unset = 0;
            shared.at.compare_exchange_strong(unset, now_ns());
            stop_reason = "coordinator_exception";
            join();
            throw;
        }
        join();
        collect();
    }

    size_t outstanding() const {
        size_t n = 0;
        for (const auto& worker : workers) n += worker->outstanding();
        return n;
    }

    void emergency_cleanup() noexcept {
        // run() always joins before propagating an exception. No other thread
        // can concurrently inspect or change a caller's slots here.
        for (auto& worker : workers) worker->emergency_cleanup();
        try { collect(); } catch (...) { unsafe = outstanding() != 0; }
        return_code = unsafe ? -4 : -2;
    }

    json counts(int phase) const {
        json total = {{"accepted", 0ull}, {"success", 0ull}, {"failure", 0ull},
                      {"pending", 0ull}, {"submitted", 0ull},
                      {"backpressure", 0ull}, {"rejected", 0ull}};
        for (const auto& worker : workers) {
            json part = worker->counts(phase);
            for (auto it = total.begin(); it != total.end(); ++it)
                it.value() = it.value().get<uint64_t>() + part.at(it.key()).get<uint64_t>();
        }
        return total;
    }

    static json timestamp(uint64_t value) { return value ? json(value) : json(nullptr); }

    void write_outputs(const std::string& directory) {
        std::ofstream output(directory + "/requests.jsonl", std::ios::trunc);
        bool output_ok = bool(output);
        if (output_ok) {
            for (const auto& worker : workers) {
              for (const auto& r : worker->records) {
                json row = {
                    {"request_id", r.epoch}, {"flow_id", r.flow_id},
                    {"phase", r.measurement ? "measurement" : "warmup"},
                    {"planned_ns", r.planned}, {"submitted_ns", timestamp(r.submitted)},
                    {"finished_ns", timestamp(r.finished)}, {"bytes", c.size},
                    {"status", r.result == 1 ? "success" : r.result == 2 ? "failure" : "pending"},
                    {"native_status", r.native_status},
                    {"source_slot", r.slot == kNoRecord ? json(nullptr) : json(r.slot)},
                    {"epoch", r.epoch}, {"transferred_bytes", r.transferred},
                    {"reason", r.reason ? json(r.reason) : json(nullptr)},
                    {"submit_rc", r.submit_rc}, {"poll_rc", r.poll_rc},
                    {"cancel_requested", r.cancel_requested}, {"cancel_rc", r.cancel_rc},
                    {"backpressured", r.queued},
                    {"free_rc", r.free_rc}};
                output << row.dump() << '\n';
                if (!output) break;
              }
              if (!output) break;
            }
            output.flush();
            output_ok = bool(output);
            output.close();
            output_ok = output_ok && !output.fail();
        }
        if (!output_ok && !unsafe) return_code = -3;
        json summary = counts(-1); // Includes warmup: pending must cover ALL DMA.
        summary["measurement"] = counts(1);
        summary["schema_version"] = 1;
        summary["counts_scope"] = "warmup_and_measurement; pending_includes_batch_cleanup";
        summary["backpressure_scope"] = "accepted_requests_that_entered_the_wait_queue";
        summary["rejected_scope"] = "first_refused_arrival_per_caller; later_stopped_input_is_not_enumerated";
        size_t queue_peak = 0;
        for (const auto& worker : workers) queue_peak += worker->queue_peak;
        summary["queue_peak"] = queue_peak;
        summary["queue_peak_scope"] = "sum_of_per_caller_peaks; upper_bound_on_simultaneous_peak";
        summary["final_epochs_scope"] = "last_successful_request_per_slot; null_if_unused_or_later_write_failed_or_pending";
        summary["clock"] = "CLOCK_MONOTONIC";
        summary["timestamp_unit"] = "ns";
        summary["measurement_start"] = timestamp(measurement_start);
        summary["measurement_end"] = timestamp(measurement_end);
        summary["measurement_start_ns"] = timestamp(measurement_start);
        summary["measurement_end_ns"] = timestamp(measurement_end);
        summary["scheduled_measurement_end_ns"] = timestamp(measurement_end);
        summary["drain_end_ns"] = timestamp(drain_end);
        summary["input_stop_ns"] = timestamp(input_stop);
        summary["elapsed"] = measurement_start && measurement_end ?
            double(measurement_end - measurement_start) / kBillion : 0.0;
        summary["warmup"] = counts(0);
        summary["warmup"]["start_ns"] = timestamp(warmup_start);
        summary["warmup"]["end_ns"] = timestamp(warmup_end);
        summary["total"] = counts(-1);
        summary["size"] = c.size;
        summary["window"] = c.window;
        summary["callers"] = c.callers;
        summary["arrival_alignment"] = c.alignment;
        summary["arrival_alignment_scope"] = "positive_rate_steps_only; rate_zero_is_closed_loop";
        summary["resource_scope"] = "one_external_engine; fixed_total_window_rate_queue";
        summary["observer_enabled"] = c.observer_enabled;
        summary["observer_symbol_found"] = observer != nullptr;
        summary["observer_prepare_seconds"] = double(c.observer_prepare) / kBillion;
        summary["warmup_drain_seconds"] = double(c.warmup_drain) / kBillion;
        summary["observer_prepare_started_ns"] = timestamp(observer_prepare_started);
        summary["observer_prepare_finished_ns"] = timestamp(observer_prepare_finished);
        summary["start_missed"] = start_missed;
        summary["timeline_scope"] = "fixed_before_observer_reset; warmup_then_fixed_drain_gap_then_measurement";
        summary["queue_capacity"] = c.queue_capacity;
        summary["max_records"] = kMaxRecords;
        summary["max_lateness_seconds"] = double(c.lateness) / kBillion;
        summary["deadline_seconds"] = double(c.deadline) / kBillion;
        summary["steps"] = json::array();
        for (const auto& step : c.steps)
            summary["steps"].push_back({{"seconds", double(step.duration) / kBillion},
                                         {"rate_bytes_per_second", step.rate}});
        summary["overflow"] = overflow;
        summary["stop_reason"] = stop_reason ? json(stop_reason) : json(nullptr);
        summary["failure_reason"] = return_code < 0 ?
            json(unsafe ? "not_drained_watchdog_required" :
                 !output_ok ? "requests_output_error" :
                 stop_reason ? stop_reason : "native_failure") : json(nullptr);
        summary["first_unaccepted_planned_ns"] = timestamp(first_unaccepted_planned);
        summary["input_stopped_early"] = measurement_start ? input_stop < measurement_end : aborting;
        summary["drained"] = !unsafe;
        summary["watchdog_required"] = unsafe;
        summary["outstanding_batches"] = outstanding();
        summary["requests_output_complete"] = output_ok;
        summary["return_code"] = return_code;
        summary["final_epochs"] = json::array();
        summary["outstanding_slots"] = json::array();
        summary["caller_summaries"] = json::array();
        for (const auto& worker : workers) {
          json caller = worker->counts(-1);
          caller["flow_id"] = worker->flow_id;
          caller["source_slot_begin"] = worker->slot_start;
          caller["source_slot_end"] = worker->slot_start + worker->slots.size();
          caller["queue_capacity"] = worker->queue.size();
          caller["queue_peak"] = worker->queue_peak;
          caller["record_capacity"] = worker->record_capacity;
          caller["measurement_start_ns"] = timestamp(worker->measurement_start);
          caller["measurement_end_ns"] = timestamp(worker->measurement_end);
          caller["input_stop_ns"] = timestamp(worker->input_stop);
          caller["drain_end_ns"] = timestamp(worker->drain_end);
          caller["stop_reason"] = worker->stop_reason ? json(worker->stop_reason) : json(nullptr);
          caller["first_unaccepted_planned_ns"] = timestamp(worker->first_unaccepted_planned);
          summary["caller_summaries"].push_back(std::move(caller));
          for (size_t i = 0; i < worker->slots.size(); ++i) {
            const auto& slot = worker->slots[i];
            summary["final_epochs"].push_back(timestamp(slot.final_epoch));
            if (slot.batch)
                summary["outstanding_slots"].push_back({{"source_slot", worker->slot_start + i},
                    {"batch_id", slot.batch}, {"request_id", worker->records[slot.record].epoch},
                    {"terminal_observed", slot.terminal}});
          }
        }
        std::ofstream report(directory + "/stream-summary.json", std::ios::trunc);
        report << summary.dump(2) << '\n';
        report.flush();
        bool report_ok = bool(report);
        report.close();
        if ((!report_ok || report.fail()) && !unsafe) return_code = -3;
    }
};
} // namespace

extern "C" int tent_test_stream(void* engine, uint64_t target_id,
                                uint64_t source_base, uint64_t target_base,
                                uint64_t stride, const char* config_json,
                                const char* output_dir) {
    std::unique_ptr<Coordinator> stream;
    std::string directory;
    try {
        if (!engine || !source_base || !target_base || !config_json ||
            !output_dir || !*output_dir)
            throw std::invalid_argument("null engine/address/config/output_dir");
        directory = output_dir;
        Config config(json::parse(config_json));
        const uint64_t max = std::numeric_limits<uint64_t>::max();
        if (config.size > max - 64 || stride < config.size + 64 ||
            config.window - 1 > (max - 64 - config.size) / stride)
            throw std::invalid_argument("stride/slot extent overflow or overlapping payloads");
        uint64_t extent = (config.window - 1) * stride + 64 + config.size;
        if (extent > std::numeric_limits<uintptr_t>::max() ||
            source_base > std::numeric_limits<uintptr_t>::max() - extent ||
            target_base > max - extent)
            throw std::invalid_argument("buffer address overflow");
        stream.reset(new Coordinator(engine, target_id, source_base, target_base,
                                stride, std::move(config)));
        try { stream->run(); }
        catch (...) { stream->emergency_cleanup(); }
        stream->write_outputs(directory);
        int rc = stream->return_code;
        if (stream->unsafe) {
            // Also retain request structs and bookkeeping, in case the C API
            // implementation still references them. Never free external buffers.
            (void)stream.release();
        }
        return rc;
    } catch (const std::exception& error) {
        if (stream) {
            if (stream->outstanding()) stream->emergency_cleanup();
            if (stream->unsafe) { (void)stream.release(); return -4; }
            return -3;
        }
        // Setup rejection never touches engine or payload memory.
        if (!directory.empty()) {
            try {
                std::ofstream report(directory + "/stream-summary.json", std::ios::trunc);
                report << json({{"return_code", -1}, {"failure_reason", error.what()},
                    {"accepted", 0}, {"success", 0}, {"failure", 0}, {"pending", 0},
                    {"measurement_start", nullptr}, {"measurement_end", nullptr},
                    {"measurement_start_ns", nullptr}, {"measurement_end_ns", nullptr},
                    {"backpressure", 0}, {"rejected", 0},
                    {"final_epochs", json::array()}, {"elapsed", 0},
                    {"overflow", false}, {"drained", true}, {"watchdog_required", false}}).dump(2)
                       << '\n';
            } catch (...) {}
        }
        return -1;
    } catch (...) {
        if (stream) {
            stream->emergency_cleanup();
            if (stream->unsafe) { (void)stream.release(); return -4; }
        }
        return -2;
    }
}
