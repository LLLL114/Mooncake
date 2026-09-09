// Experiment-only event harness. The selector implementation is compiled from
// the repository; this file models service/QP/CQ events, not RDMA hardware.
#include "hooks.h"
#include "sampling.h"
#include "production_split.h"
#include "tent/common/utils/random.h"
#include "tent/thirdparty/nlohmann/json.h"

#include <array>
#include <chrono>
#include <deque>
#include <fstream>
#include <iostream>
#include <queue>
#include <stdexcept>
#include <tuple>

using namespace mooncake::tent;
using Json = nlohmann::json;
namespace {
Json config;
struct TraceRow {
    int kind = 0, rail = 0, count = 0, priority = 0;
    uint64_t request = 0, slice = 0, bytes = 0, charged = 0;
    uint64_t time = 0, arrival = 0, post = 0, qp_posted = 0, queued = 0;
    std::array<int,8> ids{};
    std::array<double,8> scores{}, p{}, bw{}, reference{};
    std::array<uint64_t,8> inflight{};
};
std::vector<TraceRow> trace;
uint64_t current_request = 0, trace_dropped = 0, trace_limit = 0;
uint64_t digest = 1469598103934665603ULL;
DeviceSelector* live_selector = nullptr;
uint64_t metric_start=0, metric_stop=0, decision_count=0, best_changes=0;
int previous_best=-1;
double decision_tv=0;
std::array<double,8> previous_weights{};
bool have_weights=false;
void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}
void ok(const Status& status) { require(status.ok(), status.ToString()); }
void hashValue(uint64_t value) { digest = (digest ^ value) * 1099511628211ULL; }
void record(const TraceRow& row) {
    if (trace.size() < trace_limit) trace.push_back(row);
    else ++trace_dropped;
}
std::unique_ptr<DeviceSelector> selector(int rails, bool smart = true) {
    auto topo = std::make_shared<Topology>();
    Topology::MemEntry mem{};
    mem.name = "cpu:0";
    mem.type = Topology::MEM_HOST;
    mem.numa_node = config.value("memory_numa", 0);
    for (int i = 0; i < rails; ++i) {
        Topology::NicEntry nic;
        nic.name = "rail" + std::to_string(i);
        nic.type = Topology::NIC_RDMA;
        nic.numa_node = config.value("topology", std::string("equal")) == "dual_numa" ? i%2 : mem.numa_node;
        topo->nic_list_.push_back(nic);
        mem.device_list[nic.numa_node == mem.numa_node ? 0 : 1].push_back(i);
    }
    topo->mem_list_.push_back(mem);
    auto sel = std::make_unique<DeviceSelector>();
    ok(sel->loadTopology(topo));
    DeviceSelector::SchedulingParams params;
    params.bandwidth_learning_rate = config.value("alpha", 0.01);
    params.enable_priority_filtering = config.value("qos", false);
    params.score_jitter_range = config.value("jitter", 1e-9);
    sel->setSchedulingParams(params);
    sel->setSmartSelection(smart);
    for (int i = 0; i < rails; ++i) ok(sel->setDeviceBandwidth(i, 100.0));
    return sel;
}
Json stats(DeviceSelector& sel) {
    std::vector<NicLoadStats> rows;
    ok(sel.getNicLoadStats(rows));
    Json result = Json::array();
    for (const auto& row : rows)
        result.push_back({{"name", row.device_name}, {"inflight", row.inflight_bytes},
                          {"ewma", row.ewma_bandwidth_bps}});
    std::sort(result.begin(), result.end(), [](const Json& a, const Json& b) {
        return a.at("name").get<std::string>() < b.at("name").get<std::string>();
    });
    return result;
}
void captureStats(TraceRow& row, DeviceSelector& sel) {
    std::vector<NicLoadStats> data;
    ok(sel.getNicLoadStats(data));
    row.count = data.size();
    for (const auto& state : data) {
        auto id = std::stoi(state.device_name.substr(4));
        row.inflight[id] = state.inflight_bytes;
        row.bw[id] = state.ewma_bandwidth_bps;
    }
}
Json exportTrace() {
    Json result = Json::array();
    for (const auto& row : trace) {
        Json j = {{"request",row.request},{"time_ns",row.time}};
        if (row.kind == 0) {
            Json weights = Json::array(), reference = Json::array(), states = Json::array();
            for (int i=0; i<row.count; ++i) {
                if (row.ids[i]>=0) weights.push_back({{"rail",row.ids[i]},
                    {"score",row.scores[i]},{"p",row.p[i]}});
                states.push_back({{"name","rail"+std::to_string(i)},
                    {"inflight",row.inflight[i]},{"ewma",row.bw[i]}});
                reference.push_back({{"name","rail"+std::to_string(i)},{"p",row.reference[i]}});
            }
            j.update({{"event","candidates"},{"slice_bytes",row.bytes},{"priority",row.priority},
                {"weights",weights},{"stats_before",states},{"reference_weights",reference},
                {"reference_scope","64KiB cpu:0 all available rank0 NICs; no QoS/jitter"}});
        } else {
            j.update({{"slice",row.slice},{"attempt",0},{"rail",row.rail}});
            if (row.kind == 1) j.update({{"event","allocate"},{"bytes",row.bytes},
                {"charged_bytes",row.charged},{"arrival_ns",row.arrival},{"worker",0},
                {"process",0},{"location","cpu:0"},{"allocation_api","aggregate"}});
            if (row.kind == 2) j.update({{"event","post"},{"qp_posted",row.qp_posted},
                {"software_queue_slices",row.queued}});
            if (row.kind == 3) {
                Json states = Json::array();
                for (int i=0;i<row.count;++i) states.push_back({{"name","rail"+std::to_string(i)},
                    {"inflight",row.inflight[i]},{"ewma",row.bw[i]}});
                j.update({{"event","complete"},{"bytes",row.bytes},{"charged_bytes",row.charged},
                    {"post_ns",row.post},{"status","completed"},
                    {"observed_bps",row.charged/((row.time-row.post)/1e9)},{"stats_after",states}});
            }
        }
        result.push_back(std::move(j));
    }
    return result;
}
void assertDrained(DeviceSelector& sel) {
    for (const auto& row : stats(sel)) require(row.at("inflight") == 0, "inflight leak");
}
Json selftest(const std::string& name) {
    auto sel = selector(2, name != "round_robin");
    constexpr uint64_t size = 65536;
    std::vector<int> ids;
    std::vector<uint64_t> charges;
    if (name == "ewma") {
        ok(sel->chargeDevice(0, size));
        ok(sel->release(0, size, size / 5e9));
        double expected = 0.01 * 12.5e9 + 0.99 * 5e9;
        // Production lower clamp is 10% of the 100G seed.
        expected = std::max(expected, 1.25e9);
        require(std::abs(stats(*sel)[0]["ewma"].get<double>() - expected) < 1, "ewma mismatch");
    } else if (name == "clamp") {
        ok(sel->chargeDevice(0, size));
        ok(sel->release(0, size, size / 1e6));
        require(stats(*sel)[0]["ewma"].get<double>() == 1.25e9, "low clamp mismatch");
        ok(sel->chargeDevice(0, size));
        ok(sel->release(0, size, size / 1e15));
        require(stats(*sel)[0]["ewma"].get<double>() == 125e9, "high clamp mismatch");
    } else if (name == "failure_release") {
        const auto before = stats(*sel);
        ok(sel->chargeDevice(0, size));
        ok(sel->release(0, size, 0));
        require(stats(*sel) == before, "failed completion learned bandwidth");
    } else if (name == "mask") {
        ok(sel->allocate(size, 1, size, "cpu:0", ids, PRIO_HIGH, 2, &charges));
        require(ids == std::vector<int>{1}, "mask ignored");
        ok(sel->release(1, charges[0], 0));
    } else if (name == "unavailable") {
        ok(sel->setDeviceAvailable(0, false));
        ok(sel->setDeviceAvailable(1, false));
        require(!sel->allocate(size, 1, size, "cpu:0", ids).ok(), "all-dead did not fail");
        ok(sel->setDeviceAvailable(1, true));
        ok(sel->allocate(size, 1, size, "cpu:0", ids, PRIO_HIGH, ~0ULL, &charges));
        require(ids == std::vector<int>{1}, "recovery not selectable");
        ok(sel->release(1, charges[0], 0));
    } else if (name == "tail_charge") {
        // Deliberately non-divisible length: preserve the baseline's ceil
        // charging instead of silently fixing it to physical tail lengths.
        ok(sel->allocate(100001, 3, size, "cpu:0", ids, PRIO_HIGH, ~0ULL, &charges));
        require(charges == std::vector<uint64_t>({33334,33334,33334}), "tail contract changed");
        for (size_t i = 0; i < ids.size(); ++i) ok(sel->release(ids[i], charges[i], 0));
    } else if (name == "reroute") {
        ok(sel->chargeDevice(0, size));
        ok(sel->release(0, size, 0));
        ok(sel->chargeDevice(1, size));
        ok(sel->release(1, size, 0));
    } else if (name == "rounding" || name == "probe" || name == "round_robin") {
        ok(sel->setDeviceBandwidth(0, 98.0));
        ok(sel->setDeviceBandwidth(1, 102.0));
        const int calls = name == "probe" ? 100 : 1;
        for (int call = 1; call <= calls; ++call) {
            ok(sel->allocate(16*size, 16, size, "cpu:0", ids, PRIO_HIGH, ~0ULL, &charges));
            auto count0 = std::count(ids.begin(), ids.end(), 0);
            require(count0 == ((name == "round_robin" || call == 100) ? 8 : 7), "allocation mismatch");
            for (size_t i = 0; i < ids.size(); ++i) ok(sel->release(ids[i], charges[i], 0));
        }
    } else throw std::runtime_error("unknown test");
    assertDrained(*sel);
    return {{"test", name}, {"passed", true}, {"stats", stats(*sel)}};
}
struct Slice {
    uint64_t request, index, bytes, charged, arrival, post = 0;
    int rail;
    bool traced;
};
struct Completion {
    uint64_t time, serial;
    Slice slice;
    bool operator>(const Completion& b) const {
        return std::tie(time, serial) > std::tie(b.time, b.serial);
    }
};
Json estimator() {
    auto sel=selector(1);
    const double alpha=config.value("alpha",.01);
    const uint64_t samples=config.value("samples",1000000ULL);
    require(alpha>=0 && alpha<=1 && samples>1,"invalid estimator parameters");
    constexpr uint64_t bytes=65536;
    auto feed=[&](double gbps) {
        ok(sel->chargeDevice(0,bytes));
        ok(sel->release(0,bytes,bytes/(gbps*1e9)));
        return sel->getAggregateEwmaBandwidth()/1e9;
    };
    struct Moments {
        uint64_t n=0; double mean=0,m2=0;
        void add(double x) { ++n; double d=x-mean;mean+=d/n;m2+=d*(x-mean); }
        double variance() const { return m2/(n-1); }
    } input,output;
    SimpleRandom noise(config.value("seed",1U));
    uint64_t clamps=0;
    for(uint64_t i=0;i<samples+10000;++i) {
        double x=4.0+2.0*(noise.next()+0.5)/4294967296.0;
        double y=feed(x);
        if(i>=10000) { input.add(x);output.add(y); if(y<=1.25 || y>=125)++clamps; }
    }
    auto step=[&](double initial,double target) {
        ok(sel->setDeviceBandwidth(0,100.0));
        for(int i=0;i<40000;++i)feed(initial);
        int n=0; double y=0;
        do {y=feed(target);++n;} while(std::abs(y-target)>.05*std::abs(initial-target) && n<100000);
        return n;
    };
    int down=step(10.0,5.0),up=step(5.0,10.0);
    int expected=alpha==0 ? 1 : (alpha==1 ? 100000 : static_cast<int>(std::ceil(std::log(.05)/std::log(alpha))));
    return {{"source","real_DeviceSelector_estimator"},{"samples",samples},{"alpha",alpha},
        {"input_mean_GBps",input.mean},{"input_variance",input.variance()},
        {"ewma_variance",output.variance()},{"variance_ratio",output.variance()/input.variance()},
        {"theory_ratio",(1-alpha)/(1+alpha)},{"clamped_samples",clamps},
        {"down_step95_samples",down},{"up_step95_samples",up},{"expected_step95_samples",expected}};
}
Json temporalEstimator() {
    auto sel=selector(1);
    ok(sel->setDeviceBandwidth(0,80.0)); // seed 10 GB/s; samples stay inside clamps
    const uint64_t period=config.value("sample_period_ns",10000ULL);
    const uint64_t duration=config.value("sample_duration_ns",400000000ULL);
    require(period>0 && duration>=period,"invalid sample clock");
    auto values=config.value("batch_values_GBps",std::vector<double>{5.0});
    auto times=config.value("sample_times_ns",std::vector<uint64_t>{});
    if(times.empty())for(uint64_t t=period;t<=duration;t+=period)times.push_back(t);
    uint64_t first95=0,updates=0;
    constexpr uint64_t bytes=65536;
    for(uint64_t t:times) {
        experiment::now_ns=t;
        for(size_t j=0;j<values.size();++j) {
            experiment::batch_tail=j+1==values.size();
            ok(sel->chargeDevice(0,bytes));ok(sel->release(0,bytes,bytes/(values[j]*1e9)));
            ++updates;
        }
        const double value=sel->getAggregateEwmaBandwidth()/1e9;
        if(!first95 && std::abs(value-5.0)<=.25)first95=t;
    }
    assertDrained(*sel);
    return {{"source","temporal_real_estimator"},{"t95_ns",first95},
        {"final_GBps",sel->getAggregateEwmaBandwidth()/1e9},{"samples",updates},
        {"sample_meters",experiment::samplingStats(1)}};
}
Json simulate() {
    const int rails = config.value("rails", 2);
    const uint64_t requests = config.value("requests", 1000ULL);
    uint64_t slice_size = config.value("slice_bytes", 65536ULL);
    const uint64_t length = config.value("request_bytes", 1048576ULL);
    require(slice_size > 0 && length > 0, "zero slice/request length");
    uint64_t slices = (length + slice_size - 1) / slice_size;
    bool aggregate_path = true;
    if (config.value("production_split",false)) {
        auto split=productionSplit(length);
        slices=split.count; slice_size=split.bytes; aggregate_path=split.aggregate;
    }
    const uint64_t gap = config.value("arrival_interval_ns", 50000ULL);
    const uint64_t poll = config.value("poll_interval_ns", 1000ULL);
    const uint64_t latency = config.value("base_latency_ns", 1000ULL);
    const uint64_t qp_depth = config.value("qp_depth", 128ULL);
    const uint64_t sample = config.value("trace_every", 1024ULL);
    const uint64_t deadline = config.value("deadline_ns", 1000000000ULL);
    require(rails > 0 && rails <= 8 && requests > 0 && requests <= 1000000,
            "invalid rails/requests");
    require(length && slice_size && gap && poll && qp_depth && slices <= 64, "invalid model config");
    const auto capacities = config.value("capacity_bytes_per_second", std::vector<double>(rails,12.5e9));
    require(capacities.size() == size_t(rails), "capacity count mismatch");
    for (double c : capacities) require(c > 0 && std::isfinite(c), "invalid capacity");
    struct CapacityEvent { uint64_t at; int rail; double value; };
    std::vector<CapacityEvent> capacity_events;
    for(const auto& e:config.value("capacity_events",Json::array())) {
        CapacityEvent c{e.at("at_ns").get<uint64_t>(),e.at("rail").get<int>(),e.at("bytes_per_second").get<double>()};
        require(c.rail>=0 && c.rail<rails && c.value>0 && std::isfinite(c.value),"invalid capacity event");
        capacity_events.push_back(c);
    }
    std::stable_sort(capacity_events.begin(),capacity_events.end(),[](const auto& a,const auto& b){return a.at<b.at;});
    // Integrate actual wire service across changes, including a WR already
    // posted or transmitting at the change. The selector never sees this schedule.
    auto wireEnd=[&](uint64_t begin,uint64_t bytes,int rail) {
        if(capacity_events.empty()) return begin+std::max<uint64_t>(1,std::ceil(bytes/capacities[rail]*1e9));
        uint64_t t=begin; double left=bytes,rate=capacities[rail];
        for(const auto& e:capacity_events) {
            if(e.rail!=rail)continue;
            if(e.at<=t){rate=e.value;continue;}
            double available=(e.at-t)*(rate/1e9);
            if(left<=available) return t+std::max<uint64_t>(1,std::ceil(left/rate*1e9));
            left-=available;t=e.at;rate=e.value;
        }
        return t+std::max<uint64_t>(1,std::ceil(left/rate*1e9));
    };
    const uint64_t window_ns=config.value("window_ns",0ULL);
    metric_start=config.value("warmup_requests",0ULL)*gap;
    metric_stop=requests*gap;
    experiment::aggregate=window_ns>0;
    auto sel = selector(rails, config.value("smart", true));
    live_selector = sel.get();
    std::vector<std::deque<Slice>> waiting(rails);
    std::vector<std::unordered_map<uint64_t,uint64_t>> cq_groups(rails);
    std::vector<uint64_t> posted(rails), wire_free(rails), allocated(rails), completed_bytes(rails);
    std::vector<uint64_t> pending(requests, slices), latencies(requests), request_ends(requests);
    std::vector<bool> resolved(requests, false);
    std::priority_queue<Completion, std::vector<Completion>, std::greater<Completion>> cq;
    uint64_t serial = 0, now = 0, completions = 0, timed_out = 0, events = 0;
    struct Window {
        std::array<uint64_t,8> assigned{},done{};
        std::array<uint64_t,8> inflight{},queue_bytes{};
        std::array<double,8> bandwidth{},reference{};
    };
    std::vector<Window> windows(window_ns ? metric_stop/window_ns+1 : 0);
    uint64_t next_tick=window_ns;
    uint64_t selected_changes = 0, allocation_samples=0;
    double allocation_tv=0, allocation_sum=0,allocation_sum2=0;
    std::array<double,8> last_allocation{};
    std::vector<uint64_t> allocation_histogram(slices+1);
    Json request_decisions=Json::array();
    int last_first = -1;
    auto sampled = [&](uint64_t req) { return experiment::observe && sample && req % sample == 0; };
    auto start = [&](int rail) {
        while (!waiting[rail].empty() && posted[rail] < qp_depth) {
            Slice s = waiting[rail].front(); waiting[rail].pop_front();
            s.post = now;
            ++posted[rail];
            wire_free[rail] = wireEnd(std::max(now, wire_free[rail]),s.bytes,rail);
            uint64_t done = ((wire_free[rail] + latency + poll - 1) / poll) * poll;
            cq.push({done, serial++, s});
            if(experiment::sample_tail)++cq_groups[rail][done];
            if (s.traced) {
                TraceRow row; row.kind=2; row.request=s.request; row.slice=s.index;
                row.rail=rail; row.time=now; row.qp_posted=posted[rail]; row.queued=waiting[rail].size();
                record(row);
            }
        }
    };
    auto finish = [&]() {
        auto e = cq.top(); cq.pop(); now = e.time; experiment::now_ns = now;
        const auto& s = e.slice;
        require(posted[s.rail] > 0, "QP underflow");
        --posted[s.rail];
        experiment::batch_tail=true;
        if(experiment::sample_tail) {
            auto& count=cq_groups[s.rail].at(now);
            experiment::batch_tail=(--count==0);
            if(!count)cq_groups[s.rail].erase(now);
        }
        ok(sel->release(s.rail, s.charged, (now-s.post)/1e9));
        completed_bytes[s.rail] += s.bytes;
        if(window_ns && now<metric_stop) windows[now/window_ns].done[s.rail]+=s.bytes;
        ++completions; ++events;
        hashValue(now); hashValue(s.rail); hashValue(s.bytes);
        require(!resolved[s.request] && pending[s.request] > 0, "duplicate terminal");
        request_ends[s.request] = std::max(request_ends[s.request], now);
        if (--pending[s.request] == 0) {
            resolved[s.request] = true;
            latencies[s.request] = request_ends[s.request] - s.arrival;
            if (latencies[s.request] > deadline) ++timed_out;
        }
        if (s.traced) {
            TraceRow row; row.kind=3; row.request=s.request; row.slice=s.index;
            row.rail=s.rail; row.bytes=s.bytes; row.charged=s.charged; row.time=now; row.post=s.post;
            captureStats(row,*sel); record(row);
        }
        start(s.rail);
    };
    auto tick = [&]() {
        auto& window=windows[(next_tick-1)/window_ns];
        std::vector<NicLoadStats> data; ok(sel->getNicLoadStats(data));
        double total=0;
        for(const auto& state:data) {
            int id=std::stoi(state.device_name.substr(4));
            window.inflight[id]=state.inflight_bytes;
            window.bandwidth[id]=state.ewma_bandwidth_bps;
            double penalty=sel->getSchedulingParams().numa_tier_weights[sel->getDeviceRank("cpu:0",id)];
            window.reference[id]=1/(((state.inflight_bytes+65536.0)/state.ewma_bandwidth_bps)*penalty+1e-12);
            total+=window.reference[id];
        }
        for(int i=0;i<rails;++i) { window.reference[i]/=total; window.queue_bytes[i]=allocated[i]-completed_bytes[i]; }
        next_tick+=window_ns;
    };
    auto advance = [&](uint64_t until) {
        while((!cq.empty() && cq.top().time<=until) || (window_ns && next_tick<=until && next_tick<=metric_stop)) {
            if(window_ns && next_tick<=until && next_tick<=metric_stop && (cq.empty() || next_tick<=cq.top().time)) tick();
            else finish();
        }
    };
    const auto begin = std::chrono::steady_clock::now();
    for (uint64_t req = 0; req < requests; ++req) {
        uint64_t arrival = req * gap;
        advance(arrival);
        now = arrival; experiment::now_ns = now; current_request = req;
        const bool trace_request = sampled(req);
        experiment::wanted = trace_request;
        std::vector<int> ids;
        std::vector<uint64_t> charges;
        if(aggregate_path) {
            ok(sel->allocate(length,slices,slice_size,"cpu:0",ids,PRIO_HIGH,~0ULL,&charges));
        } else {
            uint64_t offset=0;
            for(uint64_t i=0;i<slices;++i) {
                uint64_t bytes=std::min(slice_size,length-offset); offset+=bytes;
                std::vector<int> one; std::vector<uint64_t> charge;
                ok(sel->allocate(bytes,1,bytes,"cpu:0",one,PRIO_HIGH,~0ULL,&charge));
                ids.push_back(one[0]); charges.push_back(charge[0]);
            }
        }
        require(ids.size() == slices && charges.size() == slices, "slice allocation size mismatch");
        if (arrival>=metric_start && last_first >= 0 && last_first != ids[0]) ++selected_changes;
        last_first = ids[0];
        uint64_t offset = 0;
        std::array<double,8> request_allocation{};
        for (uint64_t i = 0; i < slices; ++i) {
            uint64_t bytes = std::min(slice_size, length-offset); offset += bytes;
            allocated[ids[i]] += bytes;
            request_allocation[ids[i]]+=static_cast<double>(bytes)/length;
            if(window_ns) windows[arrival/window_ns].assigned[ids[i]]+=bytes;
            waiting[ids[i]].push_back({req,i,bytes,charges[i],arrival,0,ids[i],trace_request});
            hashValue(ids[i]); hashValue(charges[i]); ++events;
            if (trace_request) {
                TraceRow row; row.kind=1; row.request=req; row.slice=i; row.rail=ids[i];
                row.bytes=bytes; row.charged=charges[i]; row.time=now; row.arrival=arrival;
                record(row);
            }
        }
        require(offset == length, "byte conservation failure");
        if(arrival>=metric_start) {
            if(allocation_samples) for(int i=0;i<rails;++i)allocation_tv+=std::abs(request_allocation[i]-last_allocation[i])/2;
            last_allocation=request_allocation;++allocation_samples;
            allocation_sum+=request_allocation[0];allocation_sum2+=request_allocation[0]*request_allocation[0];
            ++allocation_histogram[std::count(ids.begin(),ids.end(),0)];
        }
        if(req>=config.value("request_trace_from",0ULL) && req<config.value("request_trace_to",0ULL)) {
            request_decisions.push_back({{"request",req},{"time_ns",now},{"weight0",previous_weights[0]},
                {"share0",request_allocation[0]},{"count0",std::count(ids.begin(),ids.end(),0)}});
        }
        for (int rail=0; rail<rails; ++rail) start(rail);
    }
    advance(metric_stop);
    while (!cq.empty()) finish();
    const auto end = std::chrono::steady_clock::now();
    for (bool done : resolved) require(done, "unresolved request");
    require(completions == requests*slices && allocated == completed_bytes, "completion conservation failure");
    assertDrained(*sel);
    auto sorted = latencies; std::sort(sorted.begin(),sorted.end());
    auto percentile = [&](double p) { return sorted[std::max<size_t>(1,std::ceil(p*sorted.size()))-1]; };
    Json window_output=Json::array();
    if(window_ns) for(size_t k=0;(k+1)*window_ns<=metric_stop;++k) {
        if(k*window_ns<config.value("window_output_from_ns",0ULL) || k*window_ns>=config.value("window_output_to_ns",UINT64_MAX)) continue;
        auto& w=windows[k];
        Json assigned=Json::array(),done=Json::array(),inflight=Json::array(),bw=Json::array(),ref=Json::array(),queue=Json::array();
        for(int i=0;i<rails;++i) {
            assigned.push_back(w.assigned[i]);done.push_back(w.done[i]);inflight.push_back(w.inflight[i]);
            bw.push_back(w.bandwidth[i]);ref.push_back(w.reference[i]);queue.push_back(w.queue_bytes[i]);
        }
        window_output.push_back({{"start_ns",k*window_ns},{"assigned",assigned},{"done",done},
            {"inflight",inflight},{"queue_bytes",queue},{"bandwidth",bw},{"reference_p",ref}});
    }
    double allocation_mean=allocation_sum/std::max<uint64_t>(1,allocation_samples);
    return {{"sample_meters",experiment::samplingStats(rails)},
        {"allocation_step",allocation_tv/(allocation_samples>1 ? allocation_samples-1 : 1)},
        {"allocation_rail0_std",std::sqrt(std::max(0.0,allocation_sum2/std::max<uint64_t>(1,allocation_samples)-allocation_mean*allocation_mean))},
        {"allocation_samples",allocation_samples},{"allocation_histogram",allocation_histogram},{"request_decisions",request_decisions},
        {"window_ns",window_ns},{"windows",window_output},{"decision_count",decision_count},
        {"best_changes",best_changes},{"decision_tv",decision_tv},
        {"split_count",slices},{"split_bytes",slice_size},{"aggregate_path",aggregate_path},
        {"source","simulation"},{"requests",requests},{"slices",completions},
        {"virtual_end_ns",now},{"allocation_bytes",allocated},{"completion_bytes",completed_bytes},
        {"p50_ns",percentile(.50)},{"p99_ns",percentile(.99)},{"max_latency_ns",sorted.back()},
        {"request_latency_ns",latencies},{"deadline_exceeded",timed_out},
        {"deadline_semantics","count-only; all modeled completions drained; not production cancellation"},
        {"first_slice_rail_changes",selected_changes},{"digest",digest},{"final_stats",stats(*sel)},
        {"events",events},{"wall_seconds",std::chrono::duration<double>(end-begin).count()},
        {"trace_dropped",trace_dropped},{"trace",exportTrace()}};
}
}
namespace experiment {
void candidates(const std::vector<DeviceSelector::Candidate>& rows, uint64_t bytes, int priority) {
    if(experiment::aggregate && now_ns>=metric_start && now_ns<metric_stop) {
        ++decision_count;
        if(previous_best>=0 && previous_best!=rows.front().dev_id) ++best_changes;
        previous_best=rows.front().dev_id;
        std::array<double,8> weights{}; double total=0;
        for(const auto& c:rows) total+=1/(c.score+1e-12);
        for(const auto& c:rows) weights[c.dev_id]=(1/(c.score+1e-12))/total;
        if(have_weights) for(size_t i=0;i<8;++i) decision_tv+=std::abs(weights[i]-previous_weights[i])/2;
        previous_weights=weights;have_weights=true;
    }
    if(!observe || !wanted) return;
    TraceRow row; row.ids.fill(-1); row.request=current_request;
    row.time=now_ns; row.bytes=bytes; row.priority=priority;
    captureStats(row,*live_selector);
    double total=0, ref_total=0;
    for (const auto& c : rows) total += 1/(c.score+1e-12);
    for (size_t i=0;i<rows.size();++i) {
        row.ids[i]=rows[i].dev_id; row.scores[i]=rows[i].score;
        row.p[i]=(1/(rows[i].score+1e-12))/total;
    }
    for (int i=0;i<row.count;++i) {
        row.reference[i]=1/((row.inflight[i]+65536.0)/row.bw[i]+1e-12);
        ref_total+=row.reference[i];
    }
    for (int i=0;i<row.count;++i) row.reference[i]/=ref_total;
    record(row);
}
}
int main(int argc, char** argv) {
    try {
        require(argc == 3, "usage: driver config.json output.json");
        std::ifstream input(argv[1]); require(bool(input),"config missing"); input >> config;
        SimpleRandom::Get() = SimpleRandom(config.value("seed",1U));
        experiment::configureSampling(config);
        trace_limit = config.value("trace_limit",100000ULL);
        experiment::observe = config.value("observe",false);
        if (experiment::observe) trace.reserve(std::min<uint64_t>(trace_limit,100000));
        Json result = config.contains("case") ? selftest(config["case"].get<std::string>()) : (config.value("estimator",false) ? estimator() : (config.value("temporal_estimator",false) ? temporalEstimator() : simulate()));
        result["config"] = config;
        std::ofstream output(argv[2]); require(bool(output),"output cannot open");
        output << result.dump() << '\n';
        return 0;
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n'; return 1;
    }
}
