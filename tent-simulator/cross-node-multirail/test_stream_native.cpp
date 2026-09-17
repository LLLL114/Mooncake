// Server-only mock: does not link TENT or access an RDMA device.
// Compile and execute only inside an SSH server, as required by AGENTS.md.
// Linux linking requires -ldl -Wl,--export-dynamic.
// Define TENT_STREAM_TEST_NO_OBSERVER to verify vanilla symbol absence.
#include "stream_native.cpp"

#include <cstdlib>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <map>
#include <set>
#include <unordered_map>
#include <unistd.h>

#define REQUIRE(condition) do { if (!(condition)) { \
    std::cerr << "FAILED " << __LINE__ << ": " << #condition << '\n'; \
    std::abort(); } } while (0)

namespace {
struct MockBatch {
    tent_request_t request{};
    std::vector<unsigned char> snapshot;
    uint64_t epoch = 0, due = 0, cancel_due = 0;
    size_t slot = 0;
    bool submitted = false, canceled = false, terminal = false, poll_error = false;
};
struct Mock {
    std::mutex lock;
    size_t callers, window, size = 512, stride = 640;
    std::vector<unsigned char> source, target;
    std::map<uint64_t, MockBatch> batches;
    std::map<size_t, std::thread::id> flow_threads;
    std::map<size_t, uint64_t> active_slots, previous_epoch;
    uint64_t next_batch = 1, delay_ns = 300000;
    uint64_t first_submit_ns = 0;
    size_t max_active = 0, submitted = 0, freed = 0, cancels = 0;
    std::string mode;
    explicit Mock(size_t n, size_t q, std::string behavior = "success")
        : callers(n), window(q), source(stride * q, 0xa5),
          target(stride * q, 0xa5), mode(std::move(behavior)) {}
};
#ifndef TENT_STREAM_TEST_NO_OBSERVER
uint64_t observer_start = 0, observer_end = 0, observer_delay_ns = 0;
#endif
int observer_calls = 0, observer_enabled = -1;
std::thread::id coordinator_thread;

uint64_t little_endian(const unsigned char* p) {
    uint64_t n = 0;
    for (unsigned b = 0; b < 8; ++b) n |= uint64_t(p[b]) << (8 * b);
    return n;
}

void validate_live(Mock& m, const MockBatch& b) {
    if (!b.submitted) return;
    const auto* source = static_cast<const unsigned char*>(b.request.source);
    REQUIRE(std::memcmp(source, b.snapshot.data(), m.size) == 0);
    REQUIRE(m.active_slots.at(b.slot) == b.epoch);
}
} // namespace

#ifndef TENT_STREAM_TEST_NO_OBSERVER
extern "C" void tent_obs_configure(int enabled, uint64_t start, uint64_t end) {
    REQUIRE(std::this_thread::get_id() == coordinator_thread);
    REQUIRE(start < end);
    ++observer_calls;
    observer_enabled = enabled;
    observer_start = start;
    observer_end = end;
    std::this_thread::sleep_for(std::chrono::nanoseconds(observer_delay_ns));
}
#endif

extern "C" tent_batch_id_t tent_allocate_batch(tent_engine_t engine, size_t size) {
    auto& m = *static_cast<Mock*>(engine);
    std::lock_guard<std::mutex> lock(m.lock);
    REQUIRE(size == 1);
    uint64_t id = m.next_batch++;
    m.batches.emplace(id, MockBatch{});
    REQUIRE(m.batches.size() <= m.window);
    m.max_active = std::max(m.max_active, m.batches.size());
    return id;
}

extern "C" int tent_submit(tent_engine_t engine, tent_batch_id_t id,
                           tent_request_t* request, size_t count) {
    auto& m = *static_cast<Mock*>(engine);
    std::lock_guard<std::mutex> lock(m.lock);
    auto& b = m.batches.at(id);
    REQUIRE(count == 1 && !b.submitted);
    if (!m.first_submit_ns) m.first_submit_ns = now_ns();
    REQUIRE(request->opcode == OPCODE_WRITE && request->transport_hint == 1);
    REQUIRE(request->priority == 0 && request->target_id == 42);
    REQUIRE(request->length == m.size);
    auto* p = static_cast<unsigned char*>(request->source);
    uintptr_t offset = reinterpret_cast<uintptr_t>(p) -
                       reinterpret_cast<uintptr_t>(m.source.data());
    REQUIRE(offset >= 64 && (offset - 64) % m.stride == 0);
    b.slot = (offset - 64) / m.stride;
    REQUIRE(b.slot < m.window && m.active_slots.count(b.slot) == 0);
    REQUIRE(request->target_offset == reinterpret_cast<uintptr_t>(m.target.data()) + offset);
    b.epoch = little_endian(p);
    REQUIRE(b.epoch && little_endian(p + m.size - 8) == b.epoch);
    REQUIRE(b.epoch > m.previous_epoch[b.slot]);
    size_t flow = (b.epoch - 1) % m.callers;
    size_t first = partition_start(m.window, m.callers, flow);
    REQUIRE(first <= b.slot && b.slot < first + partition_size(m.window, m.callers, flow));
    auto tid = std::this_thread::get_id();
    REQUIRE(tid != coordinator_thread);
    if (m.flow_threads.count(flow)) REQUIRE(m.flow_threads.at(flow) == tid);
    else {
        for (const auto& existing : m.flow_threads) REQUIRE(existing.second != tid);
        m.flow_threads.emplace(flow, tid);
    }
    for (size_t i = 8; i < m.size - 8; ++i) REQUIRE(p[i] == 0xa5);
    for (size_t i = 0; i < 64; ++i) REQUIRE(m.source[b.slot * m.stride + i] == 0xa5);
    for (size_t i = 64 + m.size; i < m.stride; ++i)
        REQUIRE(m.source[b.slot * m.stride + i] == 0xa5);
    b.request = *request;
    b.snapshot.assign(p, p + m.size);
    b.submitted = true;
    b.due = now_ns() + m.delay_ns;
    m.active_slots[b.slot] = b.epoch;
    m.previous_epoch[b.slot] = b.epoch;
    ++m.submitted;
    return m.mode == "submit_error" && b.epoch == 1 ? -1 : 0;
}

extern "C" int tent_task_status(tent_engine_t engine, tent_batch_id_t id,
                                size_t task_id, tent_status_t* status) {
    auto& m = *static_cast<Mock*>(engine);
    std::lock_guard<std::mutex> lock(m.lock);
    auto& b = m.batches.at(id);
    REQUIRE(task_id == 0 && b.submitted);
    validate_live(m, b);
    if (m.mode == "poll_error" && b.epoch == 1 && !b.poll_error) {
        b.poll_error = true;
        return -1;
    }
    status->status = STATUS_PENDING;
    status->transferred_bytes = 0;
    if (m.mode == "hang") return 0;
    uint64_t now = now_ns();
    if (b.canceled) {
        if (now < b.cancel_due) return 0;
        status->status = STATUS_CANCELED;
    } else {
        if (now < b.due) return 0;
        status->status = b.epoch == 1 && m.mode == "native_failure" ? STATUS_FAILED :
                         b.epoch == 1 && m.mode == "native_timeout" ? STATUS_TIMEOUT :
                         STATUS_COMPLETED;
        if (status->status == STATUS_COMPLETED) {
            status->transferred_bytes = m.size;
            std::memcpy(reinterpret_cast<void*>(b.request.target_offset),
                        b.snapshot.data(), m.size);
        }
    }
    b.terminal = true;
    return 0;
}

extern "C" int tent_cancel_task(tent_engine_t engine, tent_batch_id_t id, size_t task_id) {
    auto& m = *static_cast<Mock*>(engine);
    std::lock_guard<std::mutex> lock(m.lock);
    auto& b = m.batches.at(id);
    REQUIRE(task_id == 0 && !b.terminal && !b.canceled);
    validate_live(m, b);
    b.canceled = true;
    b.cancel_due = now_ns() + 200000; // Cancellation requires more polling.
    ++m.cancels;
    return 0;
}

extern "C" int tent_free_batch(tent_engine_t engine, tent_batch_id_t id) {
    auto& m = *static_cast<Mock*>(engine);
    std::lock_guard<std::mutex> lock(m.lock);
    auto& b = m.batches.at(id);
    REQUIRE(b.terminal); // Detect free-before-terminal, including cancellation.
    validate_live(m, b);
    if (m.mode == "free_error" && b.epoch == 1) return -1;
    m.active_slots.erase(b.slot);
    m.batches.erase(id);
    ++m.freed;
    return 0;
}

namespace {
struct Result { int rc; json summary; std::vector<json> rows; };

Result run_mock(Mock& m, json config, const std::filesystem::path& root,
                const std::string& label) {
    config["size"] = m.size;
    config["window"] = m.window;
    config["callers"] = m.callers;
    auto out = root / label;
    std::filesystem::create_directory(out);
    observer_calls = 0;
    observer_enabled = -1;
#ifndef TENT_STREAM_TEST_NO_OBSERVER
    observer_delay_ns = config.value("mock_observer_delay_ns", uint64_t(0));
#endif
    coordinator_thread = std::this_thread::get_id();
    std::string encoded = config.dump();
    int rc = tent_test_stream(&m, 42, reinterpret_cast<uintptr_t>(m.source.data()),
        reinterpret_cast<uintptr_t>(m.target.data()), m.stride,
        encoded.c_str(), out.c_str());
    std::ifstream summary_file(out / "stream-summary.json");
    json summary = json::parse(summary_file);
    std::ifstream request_file(out / "requests.jsonl");
    std::vector<json> rows;
    std::string line;
    while (std::getline(request_file, line)) rows.push_back(json::parse(line));
    REQUIRE(summary.at("return_code") == rc);
    REQUIRE(summary.at("accepted") == rows.size());
    REQUIRE(summary.at("success").get<size_t>() + summary.at("failure").get<size_t>() +
            summary.at("pending").get<size_t>() == rows.size());
    REQUIRE(summary.at("callers") == m.callers);
    REQUIRE(summary.at("submitted") == m.submitted);
    REQUIRE(m.max_active <= m.window);
    REQUIRE(summary.at("outstanding_batches") == m.batches.size());
    REQUIRE(summary.at("final_epochs").size() == m.window);
    if (m.first_submit_ns) {
        REQUIRE(summary.at("observer_prepare_finished_ns").get<uint64_t>() <= m.first_submit_ns);
        const auto& first_boundary = config.value("warmup", 0.0) > 0 ?
            summary.at("warmup").at("start_ns") : summary.at("measurement_start_ns");
        REQUIRE(m.first_submit_ns >= first_boundary.get<uint64_t>());
    }
    if (rc == -4) REQUIRE(summary.at("pending").get<size_t>() > 0);
    else REQUIRE(m.batches.empty() && summary.at("pending") == 0);
    std::set<uint64_t> ids;
    size_t success = 0, failure = 0, pending = 0, queued = 0;
    for (const auto& row : rows) {
        REQUIRE(ids.insert(row.at("request_id").get<uint64_t>()).second);
        REQUIRE(row.at("request_id") == row.at("epoch"));
        auto flow = row.at("flow_id").get<size_t>();
        REQUIRE(flow < m.callers);
        if (!row.at("source_slot").is_null()) {
            auto slot = row.at("source_slot").get<size_t>();
            auto first = partition_start(m.window, m.callers, flow);
            REQUIRE(first <= slot && slot < first + partition_size(m.window, m.callers, flow));
        }
        if (!row.at("submitted_ns").is_null()) {
            REQUIRE(row.at("planned_ns").get<uint64_t>() <= row.at("submitted_ns").get<uint64_t>());
            if (!row.at("finished_ns").is_null())
                REQUIRE(row.at("submitted_ns").get<uint64_t>() <= row.at("finished_ns").get<uint64_t>());
        } else REQUIRE(row.at("status") == "failure");
        success += row.at("status") == "success";
        failure += row.at("status") == "failure";
        pending += row.at("status") == "pending";
        queued += row.at("backpressured").get<bool>();
    }
    REQUIRE(summary.at("success") == success && summary.at("failure") == failure);
    REQUIRE(summary.at("pending") == pending && summary.at("backpressure") == queued);
    size_t queue_total = 0, record_total = 0, slot_total = 0;
    for (const auto& caller : summary.at("caller_summaries")) {
        queue_total += caller.at("queue_capacity").get<size_t>();
        record_total += caller.at("record_capacity").get<size_t>();
        slot_total += caller.at("source_slot_end").get<size_t>() -
                      caller.at("source_slot_begin").get<size_t>();
        REQUIRE(caller.at("measurement_start_ns") == summary.at("measurement_start_ns"));
        REQUIRE(caller.at("measurement_end_ns") == summary.at("measurement_end_ns"));
    }
    REQUIRE(queue_total == config.at("queue_capacity").get<size_t>());
    REQUIRE(record_total == kMaxRecords && slot_total == m.window);
#ifndef TENT_STREAM_TEST_NO_OBSERVER
    REQUIRE(observer_calls == 1 && summary.at("observer_symbol_found") == true);
    REQUIRE(observer_start == summary.at("measurement_start_ns").get<uint64_t>());
    REQUIRE(observer_end == summary.at("measurement_end_ns").get<uint64_t>());
    REQUIRE(observer_enabled == (config.value("observer_enabled", true) ? 1 : 0));
#else
    REQUIRE(observer_calls == 0 && summary.at("observer_symbol_found") == false);
#endif
    for (size_t slot = 0; slot < m.window; ++slot) {
        const auto& epoch = summary.at("final_epochs").at(slot);
        if (epoch.is_null()) continue;
        const auto* data = m.target.data() + slot * m.stride + 64;
        REQUIRE(little_endian(data) == epoch.get<uint64_t>());
        REQUIRE(little_endian(data + m.size - 8) == epoch.get<uint64_t>());
        for (size_t i = 8; i < m.size - 8; ++i) REQUIRE(data[i] == 0xa5);
    }
    std::cout << label << " rc=" << rc << " accepted=" << rows.size()
              << " pending=" << pending << '\n';
    return {rc, std::move(summary), std::move(rows)};
}

void verify_plan(const Result& result, size_t callers, const std::string& alignment) {
    REQUIRE(result.rc == 0);
    REQUIRE(result.summary.at("measurement").at("accepted") == 80);
    uint64_t start = result.summary.at("measurement_start_ns").get<uint64_t>();
    std::map<size_t, std::vector<uint64_t>> actual, expected;
    for (const auto& row : result.rows)
        if (row.at("phase") == "measurement")
            actual[row.at("flow_id").get<size_t>()].push_back(row.at("planned_ns").get<uint64_t>());
    for (size_t step = 0; step < 2; ++step) {
        uint64_t rps = step ? 240 : 160;
        for (uint64_t rank = 0; rank < rps / 5; ++rank) {
            uint64_t scheduled_rank = alignment == "synchronized" ? rank / callers * callers : rank;
            expected[rank % callers].push_back(start + step * 200000000 + scheduled_rank * kBillion / rps);
        }
    }
    REQUIRE(actual == expected); // Includes common steps, phases and aggregate input.
}
} // namespace

int main() {
    char temp[] = "/tmp/tent-stream-mock-XXXXXX";
    char* directory = mkdtemp(temp);
    REQUIRE(directory);
    std::filesystem::path root(directory);
    json base = {{"seconds", 0.4}, {"warmup", 0.04}, {"queue_capacity", 128},
                 {"deadline_seconds", 1}, {"max_lateness_seconds", 1},
                 {"observer_prepare_seconds", 0.1}, {"warmup_drain_seconds", 0.05},
                 {"steps", {{{"seconds", 0.2}, {"rate_bytes_per_second", 512 * 160}},
                            {{"seconds", 0.2}, {"rate_bytes_per_second", 512 * 240}}}}};
    for (size_t callers : {1, 4, 8}) {
        for (const std::string alignment : {"synchronized", "staggered"}) {
            Mock mock(callers, 32);
            json config = base;
            config["arrival_alignment"] = alignment;
            auto result = run_mock(mock, config, root, std::to_string(callers) + "-" + alignment);
            verify_plan(result, callers, alignment);
            REQUIRE(mock.flow_threads.size() == callers);
        }
    }
    {
        Mock mock(4, 10);
        json config = base;
        config["queue_capacity"] = 17;
        config["observer_enabled"] = false;
        verify_plan(run_mock(mock, config, root, "uneven-partitions-observer-off"), 4, "staggered");
    }
    json short_run = {{"seconds", 0.08}, {"warmup", 0}, {"queue_capacity", 128},
                      {"deadline_seconds", 0.2}, {"max_lateness_seconds", 1},
                      {"observer_prepare_seconds", 0.1}, {"warmup_drain_seconds", 0.05},
                      {"rate_bytes_per_second", 0}};
#ifndef TENT_STREAM_TEST_NO_OBSERVER
    {
        Mock mock(4, 16);
        json config = base;
        config["mock_observer_delay_ns"] = 40000000;
        auto result = run_mock(mock, config, root, "observer-reset-before-warmup");
        verify_plan(result, 4, "staggered");
        REQUIRE(result.summary.at("start_missed") == false);
        REQUIRE(result.summary.at("observer_prepare_finished_ns").get<uint64_t>() <
                result.summary.at("warmup").at("start_ns").get<uint64_t>());
    }
    {
        Mock mock(4, 16);
        json config = short_run;
        config["warmup"] = 0.01;
        config["observer_prepare_seconds"] = 0.03;
        config["mock_observer_delay_ns"] = 60000000;
        auto result = run_mock(mock, config, root, "observer-reset-missed-start");
        REQUIRE(result.rc == -2 && mock.submitted == 0);
        REQUIRE(result.summary.at("start_missed") == true);
        REQUIRE(result.summary.at("stop_reason") == "observer_prepare_missed_start");
        REQUIRE(result.summary.at("measurement_start_ns").get<uint64_t>() == observer_start);
    }
#endif
    {
        Mock mock(4, 16);
        mock.delay_ns = 100000000;
        json config = short_run;
        config["warmup"] = 0.01;
        config["warmup_drain_seconds"] = 0.01;
        auto result = run_mock(mock, config, root, "warmup-drain-missed-measurement");
        REQUIRE(result.rc == -2 && result.summary.at("start_missed") == true);
        REQUIRE(result.summary.at("stop_reason") == "measurement_start_missed");
        REQUIRE(result.summary.at("measurement").at("accepted") == 0);
        REQUIRE(result.summary.at("measurement_start_ns").get<uint64_t>() ==
                result.summary.at("warmup").at("end_ns").get<uint64_t>() + 10000000);
    }
    {
        Mock mock(8, 32);
        auto result = run_mock(mock, short_run, root, "closed-loop");
        REQUIRE(result.rc == 0 && mock.flow_threads.size() == 8);
    }
    {
        Mock mock(4, 8);
        mock.delay_ns = 20000000;
        json config = short_run;
        config["rate_bytes_per_second"] = 512 * 1000;
        auto result = run_mock(mock, config, root, "backpressure-drain");
        REQUIRE(result.rc == 0 && result.summary.at("backpressure").get<size_t>() > 0);
        REQUIRE(result.summary.at("measurement").at("accepted") == 80);
        REQUIRE(result.summary.at("rejected") == 0);
    }
    {
        Mock mock(4, 8);
        mock.delay_ns = kBillion;
        json config = short_run;
        config["queue_capacity"] = 8;
        config["rate_bytes_per_second"] = 512 * 100000;
        auto result = run_mock(mock, config, root, "queue-overflow");
        REQUIRE(result.rc == 1 && result.summary.at("overflow") == true);
        REQUIRE(result.summary.at("rejected").get<size_t>() >= 1);
        REQUIRE(result.summary.at("backpressure").get<size_t>() > 0);
        REQUIRE(mock.cancels > 0);
        bool unsent = false;
        for (const auto& row : result.rows)
            if (row.at("submitted_ns").is_null()) {
                REQUIRE(row.at("source_slot").is_null() && row.at("status") == "failure");
                unsent = true;
            }
        REQUIRE(unsent);
    }
    for (const std::string mode : {"native_failure", "native_timeout", "submit_error", "poll_error"}) {
        Mock mock(4, 16, mode);
        auto result = run_mock(mock, short_run, root, mode);
        REQUIRE(result.rc == -2 && result.summary.at("failure").get<size_t>() > 0);
    }
    {
        Mock mock(8, 32);
        mock.delay_ns = kBillion;
        json config = short_run;
        config["deadline_seconds"] = 0.03;
        auto result = run_mock(mock, config, root, "request-timeout-cancel-drain");
        REQUIRE(result.rc == -2 && mock.cancels > 0);
        REQUIRE(result.summary.at("stop_reason") == "request_deadline_exceeded");
    }
    for (const std::string mode : {"hang", "free_error"}) {
        Mock mock(8, 32, mode);
        json config = short_run;
        config["seconds"] = 0.01;
        config["deadline_seconds"] = 0.02;
        auto result = run_mock(mock, config, root, mode);
        REQUIRE(result.rc == -4 && result.summary.at("watchdog_required") == true);
        if (mode == "hang") REQUIRE(mock.freed == 0 && mock.cancels > 0);
    }
    std::filesystem::remove_all(root);
    std::cout << "All mock C API tests passed\n";
}
