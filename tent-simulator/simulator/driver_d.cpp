// Reuse the validated service model without copying its implementation.
#include "allocation.h"
#define main previousExperimentMain
#include "driver.cpp"
#undef main

namespace {
std::array<double,8> d_first{}, d_last{}, d_previous{};
uint64_t d_request = UINT64_MAX, d_count = 0, d_probe_count = 0, d_batch_count = 0;
uint64_t d_probe_bytes = 0, d_batch_bytes = 0, d_call = 0;
bool d_probe = false;
double d_first_tv = 0;
Json d_batches = Json::array();
}
namespace experiment {
void dCandidates(const std::vector<DeviceSelector::Candidate>& rows) {
    double total = 0;
    d_last.fill(0);
    for (const auto& c : rows) total += 1/(c.score + 1e-12);
    for (const auto& c : rows) d_last[c.dev_id] = (1/(c.score + 1e-12))/total;
    if (d_request != current_request) {
        d_request = current_request;
        d_first = d_last;
        if (now_ns >= metric_start && now_ns < metric_stop) {
            if (d_count) for (int i=0; i<8; ++i) d_first_tv += std::abs(d_first[i]-d_previous[i])/2;
            d_previous = d_first; ++d_count;
        }
    }
}
void dBatch(uint64_t call, bool probe, uint64_t bytes) {
    d_call = call; d_probe = probe;
    if (now_ns >= metric_start && now_ns < metric_stop) {
        ++d_batch_count; d_batch_bytes += bytes;
        if (probe) { ++d_probe_count; d_probe_bytes += bytes; }
    }
    if (current_request >= config.value("request_trace_from",0ULL) &&
        current_request < config.value("request_trace_to",0ULL)) {
        d_batches.push_back({{"request",current_request},{"call",call},{"probe",probe},
                             {"first_weights",d_first},{"bytes",bytes}});
    }
}
}
namespace {
Json allocationFixture() {
    const int rails = config.value("rails",2);
    require(rails>0 && rails<=8,"invalid fixture rail count");
    auto sel = selector(rails);
    live_selector = sel.get();
    const auto bandwidth = config.value("bandwidth_gbps",std::vector<double>(rails,100.0));
    const auto held = config.value("held_bytes",std::vector<uint64_t>(rails,0));
    require(bandwidth.size()==size_t(rails) && held.size()==size_t(rails),"fixture width mismatch");
    for (int i=0;i<rails;++i) { ok(sel->setDeviceBandwidth(i,bandwidth[i])); ok(sel->chargeDevice(i,held[i])); }
    auto lengths = config.value("request_lengths",std::vector<uint64_t>{1048576});
    const uint64_t calls = config.value("calls",1ULL);
    Json rows = Json::array();
    for (uint64_t k=0;k<calls;++k) {
        current_request = k; experiment::now_ns = k+1;
        d_probe = false; d_call = 0;
        uint64_t length=lengths[k%lengths.size()];
        auto split = productionSplit(length);
        if (!config.value("production_split",true)) {
            split.count = config.value("slice_count",16ULL);
            split.bytes = config.value("slice_bytes",65536ULL);
            split.aggregate = true;
        }
        std::vector<int> ids; std::vector<uint64_t> charges;
        uint64_t offset=0;
        if (split.aggregate) ok(sel->allocate(length,split.count,split.bytes,"cpu:0",ids,PRIO_HIGH,~0ULL,&charges));
        else for (uint64_t s=0;s<split.count;++s) {
            uint64_t bytes=std::min(split.bytes,length-offset); offset+=bytes;
            std::vector<int> one; std::vector<uint64_t> charge;
            ok(sel->allocate(bytes,1,bytes,"cpu:0",one,PRIO_HIGH,~0ULL,&charge));
            ids.push_back(one[0]); charges.push_back(charge[0]);
        }
        require(ids.size()==split.count && charges.size()==split.count,"fixture allocation count");
        offset=0; uint64_t zero=0, total_charge=0;
        std::vector<uint64_t> bytes(rails),counts(rails),rail_charges(rails);
        for (size_t s=0;s<ids.size();++s) {
            uint64_t n=std::min(split.bytes,length-offset); offset+=n;
            zero += n==0; bytes[ids[s]]+=n; ++counts[ids[s]];
            rail_charges[ids[s]]+=charges[s]; total_charge+=charges[s];
        }
        require(offset==length,"fixture payload conservation");
        auto snapshot=stats(*sel);
        for (int i=0;i<rails;++i) require(snapshot[i]["inflight"].get<uint64_t>()==held[i]+rail_charges[i],"fixture charge conservation");
        rows.push_back({{"request",k},{"length",length},{"slice_count",split.count},
            {"slice_bytes",split.bytes},{"aggregate",split.aggregate},{"counts",counts},
            {"payload_bytes",bytes},{"charges",rail_charges},{"total_charge",total_charge},
            {"zero_payload_slices",zero},{"first_weights",d_first},{"last_weights",d_last},
            {"call",d_call},{"probe",d_probe}});
        // No learning/service here: this is the static allocation transfer map.
        for (size_t s=0;s<ids.size();++s) ok(sel->release(ids[s],charges[s],0));
    }
    for (int i=0;i<rails;++i) ok(sel->release(i,held[i],0));
    assertDrained(*sel);
    return {{"source","real_selector_static_fixture"},{"rows",rows},{"final_stats",stats(*sel)}};
}
}
int main(int argc, char** argv) {
    try {
        require(argc==3,"usage: allocation config.json output.json");
        std::ifstream input(argv[1]); require(bool(input),"config missing"); input>>config;
        const auto mode=config.value("allocation_mode",std::string("original"));
        const std::vector<std::string> modes={"original","remainder","no_probe","frozen_greedy","refreshed_greedy"};
        auto found=std::find(modes.begin(),modes.end(),mode);
        require(found!=modes.end(),"unknown allocation mode");
        experiment::allocation_mode=found-modes.begin();
        SimpleRandom::Get()=SimpleRandom(config.value("seed",1U));
        experiment::configureSampling(config);
        trace_limit=config.value("trace_limit",100000ULL);
        experiment::observe=config.value("observe",false);
        if (experiment::observe) trace.reserve(std::min<uint64_t>(trace_limit,100000));
        Json result=config.value("allocation_fixture",false) ? allocationFixture() : simulate();
        result["config"]=config;
        result["d_request_first_weight_step"]=d_first_tv/std::max<uint64_t>(1,d_count>0 ? d_count-1 : 0);
        result["d_request_weight_samples"]=d_count;
        result["d_probe_count"]=d_probe_count; result["d_batch_count"]=d_batch_count;
        result["d_probe_bytes"]=d_probe_bytes; result["d_batch_bytes"]=d_batch_bytes;
        result["d_batches"]=d_batches;
        std::ofstream output(argv[2]); require(bool(output),"cannot open output"); output<<result.dump()<<'\n';
        return 0;
    } catch (const std::exception& e) { std::cerr<<e.what()<<'\n'; return 1; }
}
