#include "main_selection_trace.h"
#include <algorithm>
#include <cerrno>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <thread>
#include <time.h>

namespace {
constexpr size_t Capacity=262144;
std::vector<tent_osc::Record> records;
std::atomic<int> error{0};
std::atomic<unsigned> active{0};
std::atomic<uintptr_t> owner{0};
uint64_t start_ns=0,end_ns=0;
size_t count=0,dropped=0;
double recorded_epsilon=0;
thread_local main_trace::Allocation* current=nullptr;
thread_local int identity;
thread_local uint32_t request_epoch=0;
void reject(int e) { int zero=0;error.compare_exchange_strong(zero,e); }
uint64_t now() {
    timespec t{};
    if (clock_gettime(CLOCK_MONOTONIC,&t)) { reject(EIO);return 0; }
    return uint64_t(t.tv_sec)*1000000000+uint64_t(t.tv_nsec);
}
bool stop() {
    main_trace::enabled.store(false,std::memory_order_seq_cst);
    const auto deadline=now()+5000000000;
    while (active.load(std::memory_order_seq_cst)) {
        if (now()>deadline) { reject(EBUSY);return false; }
        std::this_thread::yield();
    }
    return true;
}
}

namespace main_trace {
std::atomic<bool> enabled{false};
RequestScope::RequestScope(const void* source,uint64_t length) noexcept : previous(request_epoch) {
    request_epoch=0;
    if (!enabled.load(std::memory_order_relaxed)) return;
    // The native CPU benchmark writes an epoch before submit and retains the
    // slot until completion. Never enable this observer for GPU buffers.
    if (!source || length!=1048576) { reject(EINVAL);return; }
    uint64_t epoch=0;std::memcpy(&epoch,source,8);
    if (!epoch || epoch>UINT32_MAX) { reject(ERANGE);return; }
    request_epoch=static_cast<uint32_t>(epoch);
}
RequestScope::~RequestScope() { request_epoch=previous; }
Allocation::Allocation(const void* selector,uint64_t total,uint32_t slices,uint64_t block_bytes,
                       double eps,const std::vector<int>& output) noexcept
    : result(output),block(block_bytes),epsilon(eps) {
    if (!enabled.load(std::memory_order_seq_cst)) return;
    ::active.fetch_add(1,std::memory_order_seq_cst);
    const auto ns=now();
    if (!enabled.load(std::memory_order_seq_cst) || ns<start_ns || ns>=end_ns) {
        ::active.fetch_sub(1,std::memory_order_seq_cst);return;
    }
    const auto me=reinterpret_cast<uintptr_t>(&identity);
    uintptr_t zero=0;owner.compare_exchange_strong(zero,me);
    if (owner.load()!=me || current) {
        reject(ENOTSUP);::active.fetch_sub(1,std::memory_order_seq_cst);return;
    }
    record.ns=ns;record.total_bytes=total;record.selector=reinterpret_cast<uintptr_t>(selector);
    if (!count) recorded_epsilon=eps;
    else if (recorded_epsilon!=eps) reject(EINVAL);
    record.slices=slices;record.mode=slices==1 ? 0 : 1;
    record.context=request_epoch;record.site=request_epoch ? 1 : 0;
    active=true;current=this;
}
Allocation::~Allocation() {
    if (!active) return;
    if (!ok || candidates!=2 || !record.slices || result.size()!=record.slices || !block) {
        reject(EINVAL);
    } else {
        uint64_t offset=0;
        for (size_t i=0;i<result.size();++i) {
            int slot=result[i]==record.dev[0] ? 0 : result[i]==record.dev[1] ? 1 : -1;
            if (slot<0 || offset>=record.total_bytes) { reject(EINVAL);break; }
            const uint64_t bytes=i+1==result.size() ? record.total_bytes-offset : std::min(block,record.total_bytes-offset);
            record.assigned[slot]+=bytes;offset+=bytes;
        }
        if (offset!=record.total_bytes) reject(EINVAL);
        if (record.mode==2) { // Diagnostic inverse score, not consumed probe proportions.
            const double a=1./(record.score[0]+epsilon),b=1./(record.score[1]+epsilon);
            record.weight[0]=a/(a+b);record.weight[1]=b/(a+b);
        }
        if (count<records.size()) { record.sequence=count;records[count++]=record; }
        else { ++dropped;reject(EOVERFLOW); }
    }
    current=nullptr;::active.fetch_sub(1,std::memory_order_seq_cst);
}
void candidate(int dev,double score,uint64_t inflight,double bandwidth,double penalty) noexcept {
    if (!current) return;
    auto& s=*current;auto& r=s.record;
    if (s.candidates>=2) { reject(EOVERFLOW);return; }
    const unsigned i=s.candidates++;
    r.dev[i]=dev;r.score[i]=score;r.inflight[i]=inflight;r.bandwidth[i]=bandwidth;r.penalty[i]=penalty;
    if (s.candidates==2 && r.dev[0]>r.dev[1]) {
        std::swap(r.dev[0],r.dev[1]);std::swap(r.score[0],r.score[1]);
        std::swap(r.inflight[0],r.inflight[1]);std::swap(r.bandwidth[0],r.bandwidth[1]);
        std::swap(r.penalty[0],r.penalty[1]);
    }
}
void weight(int dev,double inverse,double total) noexcept {
    if (!current) return;
    auto& r=current->record;
    const int i=dev==r.dev[0] ? 0 : dev==r.dev[1] ? 1 : -1;
    if (i<0 || total<=0) { reject(EINVAL);return; }
    r.weight[i]=inverse/total;
}
void probe(bool value) noexcept { if (current) current->record.mode=value ? 2 : 1; }
void success() noexcept { if (current) current->ok=true; }
}

extern "C" void tent_obs_configure(int on,uint64_t begin,uint64_t end) noexcept {
    if (!stop() || !on) return;
    if (begin>=end) { reject(EINVAL);return; }
    try { records.resize(Capacity); } catch (...) { reject(ENOMEM);return; }
    count=0;dropped=0;owner.store(0);error.store(0);start_ns=begin;end_ns=end;
    main_trace::enabled.store(true,std::memory_order_seq_cst);
}
extern "C" int tent_obs_last_error() noexcept { return error.load(); }
extern "C" void tent_obs_dump(const char* path) noexcept {
    if (!path || !stop()) return;
    try {
        const auto root=std::filesystem::path(path).parent_path();
        std::ofstream binary(root/"main-selection.bin",std::ios::binary);
        binary.write(reinterpret_cast<const char*>(records.data()),count*sizeof(tent_osc::Record));
        binary.close();if (!binary) reject(EIO);
        std::ofstream meta(path);
        meta<<std::setprecision(17)<<"{\"schema\":\"tent-main-selection-v1\",\"clock\":\"CLOCK_MONOTONIC\","
            <<"\"record_bytes\":152,\"count\":"<<count<<",\"dropped\":"<<dropped
            <<",\"epsilon\":"<<recorded_epsilon
            <<",\"start_ns\":"<<start_ns<<",\"end_ns\":"<<end_ns
            <<",\"error\":"<<error.load()<<",\"incomplete\":"<<(error.load()?"true":"false")
            <<",\"scope\":\"context stores native CPU request epoch; no CQ hooks; probes carry diagnostic inverse-score weights\"}\n";
        meta.close();if (!meta) reject(EIO);
    } catch (...) { reject(EIO); }
}
