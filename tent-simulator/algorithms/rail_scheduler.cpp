#include "rail_scheduler.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>
#include <stdexcept>

namespace mooncake::tent::multirail {
namespace {
void require(bool condition,const char* text) { if(!condition)throw std::invalid_argument(text); }
void validate(const std::vector<Candidate>& c,const std::vector<uint64_t>& s) {
    require(!c.empty() && c.size()<=64 && !s.empty(),"empty/oversized allocation");
    uint64_t mask=0,total=0;
    for(const auto& r:c) {
        require(r.id>=0 && r.id<64 && !(mask&(1ULL<<r.id)),"duplicate/invalid rail");
        mask|=1ULL<<r.id;
        require(std::isfinite(r.score) && r.score>0 && std::isfinite(r.nominal_bytes_per_second) && r.nominal_bytes_per_second>0 && std::isfinite(r.numa_penalty) && r.numa_penalty>=1,"invalid candidate");
    }
    for(auto bytes:s){require(bytes<=UINT64_MAX-total,"byte overflow");total+=bytes;}
    require(total>0,"zero request");
}
std::vector<size_t> scoreOrder(const std::vector<Candidate>& c) {
    std::vector<size_t> order(c.size());std::iota(order.begin(),order.end(),0);
    std::sort(order.begin(),order.end(),[&](size_t a,size_t b) {
        return c[a].score!=c[b].score ? c[a].score<c[b].score : c[a].id<c[b].id;
    });return order;
}
}
Assignment allocateLargestRemainder(const std::vector<Candidate>& c,const std::vector<uint64_t>& slices,bool probe) {
    validate(c,slices);Assignment out;out.bytes=slices;out.ids.reserve(slices.size());auto order=scoreOrder(c);
    double norm=0;for(const auto& r:c)norm+=1/(r.score+1e-12);for(const auto& r:c)out.weights.push_back((1/(r.score+1e-12))/norm);
    if(probe){for(size_t s=0;s<slices.size();++s)out.ids.push_back(c[order[s%order.size()]].id);return out;}
    if(slices.size()==1){out.ids.push_back(c[order[0]].id);return out;}
    double total=0;std::vector<double> exact(c.size());for(const auto& r:c)total+=1/(r.score+1e-12);
    size_t remaining=slices.size();
    for(size_t i:order){exact[i]=(1/(c[i].score+1e-12))/total*slices.size();size_t n=std::min(remaining,static_cast<size_t>(std::floor(exact[i])));remaining-=n;out.ids.insert(out.ids.end(),n,c[i].id);}
    std::sort(order.begin(),order.end(),[&](size_t a,size_t b){double x=exact[a]-std::floor(exact[a]),y=exact[b]-std::floor(exact[b]);return x!=y?x>y:c[a].id<c[b].id;});
    require(remaining<=order.size(),"invalid integer remainder");for(size_t i=0;i<remaining;++i)out.ids.push_back(c[order[i]].id);
    return out;
}
Scheduler::Scheduler(Policy p,Parameters parameters):policy_(p),parameters_(parameters) {
    require(parameters_.capacity_tau_ns && parameters_.target_tau_ns && parameters_.target_interval_ns && parameters_.sample_min_ns && parameters_.sample_min_bytes && parameters_.qp_limit,"invalid time/resource parameter");
    require(parameters_.probe_fraction>=0 && parameters_.probe_fraction<=.1 && parameters_.target_hysteresis>=0 && parameters_.max_target_step>0 && parameters_.max_target_step<=1 && parameters_.congestion_ratio>=0,"invalid control parameter");
}
Assignment Scheduler::allocate(const std::vector<Candidate>& c,const std::vector<uint64_t>& slices,uint64_t now,uint64_t group,bool probe) {
    validate(c,slices);std::lock_guard<std::mutex> lock(mutex_);
    uint64_t bytes=0,max_slice=0;for(auto s:slices){bytes+=s;max_slice=std::max(max_slice,s);}
    for(const auto& r:c)require(bytes<=UINT64_MAX-rails_[r.id].stats.reserved_bytes,"reservation overflow");
    for(const auto& r:c){auto& state=rails_[r.id];if(state.nominal!=r.nominal_bytes_per_second){state.nominal=r.nominal_bytes_per_second;state.stats.capacity=state.nominal;}}
    if(policy_==Policy::LargestRemainder){auto out=allocateLargestRemainder(c,slices,probe);for(size_t s=0;s<slices.size();++s)reserve(out.ids[s],slices[s],now);return out;}
    probe_credit_=std::min(2.0*max_slice,probe_credit_+parameters_.probe_fraction*bytes);
    return policy_==Policy::EarliestFinish ? allocateEarliestFinish(c,slices,now) : allocateByteDeficit(c,slices,now,group);
}
void Scheduler::reserve(int id,uint64_t bytes,uint64_t now) {
    auto& s=rails_[id].stats;require(bytes<=UINT64_MAX-s.reserved_bytes,"reservation overflow");s.reserved_bytes+=bytes;s.last_sent_ns=std::max(s.last_sent_ns,now);
}
double Scheduler::finishCost(const Candidate& c,uint64_t bytes) const {
    const auto& s=rails_[c.id].stats;
    double pressure=std::max(0.0,(double(s.posted)/parameters_.qp_limit-.75)/.25);
    // Affinity is a bounded preference for this new slice, not a multiplier
    // on all previously queued work. v1's multiplicative queue penalty caused
    // large NUMA tail-latency regressions despite smoother allocations.
    double preference=std::min(double(parameters_.affinity_slack_ns),
                               (c.numa_penalty-1)*double(bytes)/s.capacity*1e9);
    return (double(s.reserved_bytes)+bytes)/s.capacity*1e9+preference+s.latency_ns+s.latency_ns*pressure*pressure;
}
int Scheduler::probeCandidate(const std::vector<Candidate>& c,uint64_t bytes,uint64_t now) const {
    if(!parameters_.probe_interval_ns || bytes==0 || probe_credit_<bytes)return -1;
    int result=-1;uint64_t oldest=now;
    for(size_t i=0;i<c.size();++i){const auto& s=rails_[c[i].id].stats;if(now>=s.last_sent_ns && now-s.last_sent_ns>=parameters_.probe_interval_ns && s.last_sent_ns<oldest){oldest=s.last_sent_ns;result=i;}}
    return result;
}
Assignment Scheduler::allocateEarliestFinish(const std::vector<Candidate>& c,const std::vector<uint64_t>& slices,uint64_t now) {
    Assignment out;out.bytes=slices;out.ids.reserve(slices.size());
    double norm=0;for(const auto& r:c)norm+=1/(finishCost(r,slices.front())+1e-3);for(const auto& r:c)out.weights.push_back((1/(finishCost(r,slices.front())+1e-3))/norm);
    for(size_t s=0;s<slices.size();++s){int best=0;double cost=finishCost(c[0],slices[s]);
        for(size_t i=1;i<c.size();++i){double v=finishCost(c[i],slices[s]);if(v<cost || (v==cost && c[i].id<c[best].id)){best=i;cost=v;}}
        int probe=s==0?probeCandidate(c,slices[s],now):-1;if(probe>=0){best=probe;probe_credit_-=slices[s];}
        out.ids.push_back(c[best].id);reserve(c[best].id,slices[s],now);
    }return out;
}
Assignment Scheduler::allocateByteDeficit(const std::vector<Candidate>& c,const std::vector<uint64_t>& slices,uint64_t now,uint64_t group) {
    auto& t=targets_[group];uint64_t mask=0,bytes=0,max_slice=0;double sum=0;
    std::array<double,64> desired{};
    for(const auto& r:c){mask|=1ULL<<r.id;desired[r.id]=rails_[r.id].stats.capacity/r.numa_penalty;sum+=desired[r.id];}
    for(const auto& r:c)desired[r.id]/=sum;
    if(t.mask!=mask){t.weight=desired;t.credit.fill(0);t.mask=mask;t.last_update=now;}
    else if(now>=t.last_update && now-t.last_update>=parameters_.target_interval_ns){
        double tv=0;for(const auto& r:c)tv+=std::abs(desired[r.id]-t.weight[r.id])/2;
        if(tv>parameters_.target_hysteresis){double gain=std::min(1-std::exp(-double(now-t.last_update)/parameters_.target_tau_ns),parameters_.max_target_step/tv);
            for(const auto& r:c)t.weight[r.id]+=gain*(desired[r.id]-t.weight[r.id]);}
        t.last_update=now;
    }
    for(auto s:slices){bytes+=s;max_slice=std::max(max_slice,s);}
    double limit=2.0*bytes+2.0*max_slice;
    for(const auto& r:c)t.credit[r.id]=std::clamp(t.credit[r.id]+t.weight[r.id]*bytes,-limit,limit);
    Assignment out;out.bytes=slices;out.ids.reserve(slices.size());for(const auto& r:c)out.weights.push_back(t.weight[r.id]);
    for(size_t s=0;s<slices.size();++s){size_t debt=0,fast=0;double best=finishCost(c[0],slices[s]);
        for(size_t i=1;i<c.size();++i){double v=finishCost(c[i],slices[s]);if(v<best || (v==best && c[i].id<c[fast].id)){fast=i;best=v;}
            if(t.credit[c[i].id]>t.credit[c[debt].id] || (t.credit[c[i].id]==t.credit[c[debt].id] && c[i].id<c[debt].id))debt=i;}
        if(finishCost(c[debt],slices[s])>best+std::max(double(parameters_.congestion_slack_ns),parameters_.congestion_ratio*best))debt=fast;
        int probe=s==0?probeCandidate(c,slices[s],now):-1;if(probe>=0){debt=probe;probe_credit_-=slices[s];}
        int id=c[debt].id;out.ids.push_back(id);reserve(id,slices[s],now);t.credit[id]-=slices[s];
    }return out;
}
void Scheduler::posted(int id,uint64_t bytes,uint64_t now) {
    require(id>=0 && id<64,"invalid posted rail");std::lock_guard<std::mutex> lock(mutex_);auto& r=rails_[id];
    require(r.stats.reserved_bytes>=r.stats.posted_bytes && bytes<=r.stats.reserved_bytes-r.stats.posted_bytes,"post without unposted reservation");
    r.stats.posted_bytes+=bytes;
    if(r.stats.posted++==0){r.first_post=now;r.first_pending=true;r.window_started=false;r.window_bytes=0;}
}
void Scheduler::completed(int id,uint64_t bytes,uint64_t post,uint64_t now,bool was_posted,bool success) {
    require(id>=0 && id<64,"invalid completed rail");std::lock_guard<std::mutex> lock(mutex_);auto& r=rails_[id];
    require(r.stats.reserved_bytes>=bytes,"reservation underflow");
    if(was_posted)require(r.stats.posted>0 && r.stats.posted_bytes>=bytes && now>=post,"posted count/time underflow");
    else require(r.stats.reserved_bytes>=r.stats.posted_bytes && bytes<=r.stats.reserved_bytes-r.stats.posted_bytes,"cancel of posted bytes");
    bool software_backlogged=r.stats.reserved_bytes>r.stats.posted_bytes;
    r.stats.reserved_bytes-=bytes;
    if(!was_posted)return;
    --r.stats.posted;r.stats.posted_bytes-=bytes;
    uint64_t elapsed=now-post;now=std::max(now,r.last_event);r.last_event=now;
    if(policy_==Policy::LargestRemainder || !success){r.window_started=false;if(!success)r.first_pending=false;return;}
    if(r.first_pending && post==r.first_post){double residual=std::clamp(double(elapsed)-double(bytes)/r.stats.capacity*1e9,0.0,1000000.0);
        r.stats.latency_ns=r.latency_initialized?.9*r.stats.latency_ns+.1*residual:residual;r.latency_initialized=true;r.first_pending=false;}
    // Start on first completion, not first post: a fixed CQ/propagation delay
    // must not be divided into the capacity estimate on every idle burst.
    bool same_burst=post>=r.window_post && post-r.window_post<=parameters_.sample_burst_span_ns;
    if(!r.window_started || (!software_backlogged && !same_burst)) {
        r.window_start=now;r.window_post=post;r.window_bytes=0;r.window_started=true;
    }
    else {
        r.window_bytes+=bytes;uint64_t dt=now-r.window_start;
        if(dt>=parameters_.sample_min_ns && r.window_bytes>=parameters_.sample_min_bytes){
            double sample=std::clamp(double(r.window_bytes)/dt*1e9,r.nominal*.05,r.nominal);
            sample=std::clamp(sample,r.stats.capacity*.5,r.stats.capacity*2);
            double gain=1-std::exp(-double(now-r.last_update)/parameters_.capacity_tau_ns);
            r.stats.capacity+=gain*(sample-r.stats.capacity);++r.stats.updates;r.last_update=now;r.window_start=now;r.window_post=post;r.window_bytes=0;
        }
    }
    if(!r.stats.posted)r.window_started=false;
}
std::array<RailStats,64> Scheduler::stats() const {
    std::lock_guard<std::mutex> lock(mutex_);std::array<RailStats,64> out;for(size_t i=0;i<out.size();++i)out[i]=rails_[i].stats;return out;
}
} // namespace mooncake::tent::multirail
