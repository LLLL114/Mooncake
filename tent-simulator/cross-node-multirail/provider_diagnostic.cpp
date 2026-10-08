#include "provider_diagnostic.h"
#include "poll_diagnostic.h"
#include <cerrno>
#include <fstream>
#include <string>

namespace tent_provider {
thread_local bool entered=false;
namespace {
struct Counters { uint64_t outer=0, backend=0, skipped=0, empty=0, full=0, completions=0; };
struct alignas(128) Worker { Counters cq[64]; };
Worker workers[6];
}
void poll(int worker,int dev,uint64_t begin,uint64_t end,int count) noexcept {
    if (worker<0 || worker>=6 || dev<0 || dev>=64 || count<0 || count>64) {
        tent_pd::reject(EINVAL); return;
    }
    auto& c=workers[worker].cq[dev]; ++c.outer;
    if (!entered) {
        ++c.skipped;
        if (count) tent_pd::reject(EIO);
        return; // A quota-zero return is not evidence of servicing the CQ.
    }
    ++c.backend; c.empty+=count==0; c.full+=count==64; c.completions+=count;
    tent_pd::poll(worker,dev,begin,end,count);
}
void dump(const char* directory) {
    std::ofstream out(std::string(directory)+"/provider-summary.json");
    out<<"{\"schema\":\"tent-provider-counters-v1\",\"rows\":[";
    bool first=true;
    for (int w=0;w<6;++w) for (int d=0;d<64;++d) {
        const auto& c=workers[w].cq[d]; if (!c.outer) continue;
        if (!first) out<<','; first=false;
        out<<"{\"worker\":"<<w<<",\"dev\":"<<d<<",\"outer\":"<<c.outer
           <<",\"backend\":"<<c.backend<<",\"skipped\":"<<c.skipped
           <<",\"empty\":"<<c.empty<<",\"full\":"<<c.full
           <<",\"completions\":"<<c.completions<<'}';
    }
    out<<"]}\n"; out.close(); if (!out) tent_pd::reject(EIO);
}
}
