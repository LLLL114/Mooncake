#include "poll_diagnostic.h"
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cstring>
#include <fstream>
#include <memory>
#include <string>
#include <time.h>

namespace {
constexpr unsigned Workers=6, Devices=64, MaxCompletions=1048576, MaxGaps=131072;
struct Sync { uint64_t before=0,real=0,after=0; };
uint64_t now(clockid_t id) {
    timespec ts{};
    if (clock_gettime(id,&ts)) { tent_pd::reject(EIO); return 0; }
    return uint64_t(ts.tv_sec)*1000000000+uint64_t(ts.tv_nsec);
}
Sync sync_clock() {
    Sync s; s.before=now(CLOCK_MONOTONIC);s.real=now(CLOCK_REALTIME);s.after=now(CLOCK_MONOTONIC);return s;
}
Sync initial_sync;
struct Poll {
    uint64_t begin=0,end=0,drain_begin=0,drain_end=0;
    uint64_t previous_begin=0,previous_end=0,previous_drain_begin=0,previous_drain_end=0;
    uint64_t calls=0,full=0,max_call=0,max_gap=0;
};
struct alignas(128) Data {
    std::unique_ptr<tent_pd::Completion[]> completions;
    std::unique_ptr<tent_pd::Gap[]> gaps;
    Poll polls[Devices];
    uint64_t loop=0,phase=0,inflight=0;
    unsigned count=0,gap_count=0;
    Sync syncs[2048];
    unsigned sync_count=0;
    uint64_t last_sync=0;
};
Data data[Workers];
std::atomic<int> error{0};
bool dumped=false;
bool worker_ok(int w) { return w>=0 && w<int(Workers); }
}
namespace tent_pd {
bool active=false;
void reject(int code) noexcept { int zero=0; error.compare_exchange_strong(zero,code); }
void cycle(int w,uint64_t now,uint64_t inflight) noexcept {
    if (!worker_ok(w)) { reject(EINVAL); return; }
    data[w].loop=now; data[w].inflight=inflight;
}
void phase(int w,uint64_t now) noexcept {
    if (!worker_ok(w)) { reject(EINVAL); return; }
    data[w].phase=now;
}
void poll(int w,int dev,uint64_t begin,uint64_t end,int count) noexcept {
    if (!worker_ok(w) || dev<0 || dev>=int(Devices) || count<0 || count>64 || end<begin) {
        reject(EINVAL); return;
    }
    auto& d=data[w]; auto& p=d.polls[dev];
    if (!d.last_sync || end-d.last_sync>=1000000000) {
        if (d.sync_count<2048) d.syncs[d.sync_count++]=sync_clock();
        else reject(EOVERFLOW);
        d.last_sync=end;
    }
    if (p.end && begin<p.end) { reject(ERANGE); return; }
    p.previous_begin=p.begin; p.previous_end=p.end;
    p.previous_drain_begin=p.drain_begin; p.previous_drain_end=p.drain_end;
    const uint64_t gap=p.end ? end-p.end : 0;
    if (p.end && gap>=1000000) {
        if (d.gap_count<MaxGaps) d.gaps[d.gap_count++]={p.end,begin,end,d.loop,d.phase,d.inflight,uint64_t(dev),uint64_t(count)};
        else reject(EOVERFLOW);
    }
    p.begin=begin; p.end=end; ++p.calls; p.full+=count==64;
    p.max_call=std::max(p.max_call,end-begin); p.max_gap=std::max(p.max_gap,gap);
    if (count<64) { p.drain_begin=begin; p.drain_end=end; }
}
int complete(int w,int dev,int qp,const void* source,uint64_t size,const void* slice_source,
             uint64_t bytes,uint64_t enqueue,uint64_t submit) noexcept {
    if (!worker_ok(w) || dev<0 || dev>=int(Devices) || qp<0 || !source || size!=1048576 || bytes!=65536) {
        reject(EINVAL); return -1;
    }
    const auto base=reinterpret_cast<uintptr_t>(source), address=reinterpret_cast<uintptr_t>(slice_source);
    if (address<base || address-base>size-bytes || (address-base)%65536) { reject(EINVAL); return -1; }
    uint64_t epoch=0; std::memcpy(&epoch,source,8); // Native CPU test writes little-endian epoch before submit.
    auto& d=data[w]; auto& p=d.polls[dev];
    if (!epoch || !enqueue || enqueue>submit || submit>p.end || !p.calls) { reject(ERANGE); return -1; }
    if (d.count>=MaxCompletions) { reject(EOVERFLOW); return -1; }
    const unsigned token=d.count++;
    d.completions[token]={epoch,address-base,bytes,enqueue,submit,p.begin,p.end,
        p.previous_begin,p.previous_end,p.previous_drain_begin,p.previous_drain_end,
        d.loop,d.phase,0,uint32_t(w),uint32_t(dev),uint32_t(qp),0};
    return int(token);
}
void handled(int w,int token,uint64_t now) noexcept {
    if (!worker_ok(w) || token<0 || unsigned(token)>=data[w].count) { reject(EINVAL); return; }
    auto& r=data[w].completions[token];
    if (r.handled || now<r.poll_end) { reject(ERANGE); return; }
    r.handled=now;
}
}
extern "C" int tent_pd_configure() noexcept {
    if (tent_pd::active) return -EALREADY;
    try {
        for (auto& d:data) {
            d.completions=std::make_unique<tent_pd::Completion[]>(MaxCompletions);
            d.gaps=std::make_unique<tent_pd::Gap[]>(MaxGaps);
        }
    } catch (...) { return -ENOMEM; }
    initial_sync=sync_clock(); tent_pd::active=true; return 0;
}
extern "C" int tent_pd_dump(const char* directory) noexcept {
    if (!directory || !tent_pd::active || dumped) return -EINVAL;
    dumped=true;
    try {
        const std::string root(directory);
        std::ofstream meta(root+"/poll-diagnostic.json");
        meta<<"{\"schema\":\"tent-poll-diagnostic-v1\",\"clock\":\"CLOCK_REALTIME\",\"clock_samples\":[";
        auto write_sync=[&](const Sync& s) { meta<<'['<<s.before<<','<<s.real<<','<<s.after<<']'; };
        write_sync(initial_sync);meta<<',';write_sync(sync_clock());
        for (const auto& d:data) for (unsigned i=0;i<d.sync_count;++i) { meta<<',';write_sync(d.syncs[i]); }
        meta<<"],\"completion_format\":\"<14Q4I\",\"completion_bytes\":128,\"gap_format\":\"<8Q\",\"gap_bytes\":64,\"workers\":[";
        for (unsigned w=0;w<Workers;++w) {
            auto& d=data[w]; if (w) meta<<',';
            for (unsigned i=0;i<d.count;++i) if (!d.completions[i].handled) tent_pd::reject(EIO);
            const std::string c="completions-"+std::to_string(w)+".bin",g="poll-gaps-"+std::to_string(w)+".bin";
            std::ofstream co(root+"/"+c,std::ios::binary),go(root+"/"+g,std::ios::binary);
            co.write(reinterpret_cast<const char*>(d.completions.get()),d.count*sizeof(tent_pd::Completion));
            go.write(reinterpret_cast<const char*>(d.gaps.get()),d.gap_count*sizeof(tent_pd::Gap));
            co.close();go.close();if (!co || !go) tent_pd::reject(EIO);
            meta<<"{\"worker\":"<<w<<",\"completions\":"<<d.count<<",\"gaps\":"<<d.gap_count<<",\"completion_file\":\""<<c<<"\",\"gap_file\":\""<<g<<"\",\"polls\":[";
            bool first=true;
            for (unsigned dev=0;dev<Devices;++dev) {
                const auto& p=d.polls[dev]; if (!p.calls) continue;
                if (!first) meta<<',';
                first=false;
                meta<<"{\"dev\":"<<dev<<",\"calls\":"<<p.calls<<",\"full\":"<<p.full<<",\"max_call_ns\":"<<p.max_call<<",\"max_gap_ns\":"<<p.max_gap<<'}';
            }
            meta<<"]}";
        }
        meta<<"],\"error\":"<<error.load()<<"}\n";meta.close();
        return meta ? -error.load() : -EIO;
    } catch (...) { return -EIO; }
}
