#pragma once
#include "hooks.h"
#include "tent/thirdparty/nlohmann/json.h"
#include <stdexcept>
#include "rail_scheduler.h"
#include <memory>
#include <unordered_map>
#include <chrono>
#include <atomic>
namespace experiment {
namespace mr=mooncake::tent::multirail;
inline int candidate_policy=0;
inline std::unordered_map<const mooncake::tent::DeviceSelector*,std::unique_ptr<mr::Scheduler>> policy_owners;
inline std::atomic<uint64_t> policy_calls{0},policy_ns{0},allocation_calls{0},allocation_ns{0};
struct AllocationTimer {
    std::chrono::steady_clock::time_point begin=std::chrono::steady_clock::now();
    ~AllocationTimer(){allocation_ns.fetch_add(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-begin).count(),std::memory_order_relaxed);allocation_calls.fetch_add(1,std::memory_order_relaxed);}
};
void policyDecision(const std::vector<mr::Candidate>&,const mr::Assignment&);
inline void registerPolicy(mooncake::tent::DeviceSelector* owner,const nlohmann::json& cfg) {
    auto name=cfg.value("new_policy",std::string("legacy"));
    if(name=="legacy"){candidate_policy=0;return;}
    if(name=="largest_remainder")candidate_policy=1;
    else if(name=="earliest_finish")candidate_policy=2;
    else if(name=="byte_deficit")candidate_policy=3;
    else throw std::invalid_argument("unknown new_policy");
    if(!cfg.value("smart",true))throw std::invalid_argument("new_policy requires smart=true");
    mr::Parameters p;
    p.capacity_tau_ns=cfg.value("capacity_tau_ns",p.capacity_tau_ns);
    p.target_tau_ns=cfg.value("target_tau_ns",p.target_tau_ns);
    p.probe_interval_ns=cfg.value("probe_interval_ns",p.probe_interval_ns);
    p.congestion_slack_ns=cfg.value("congestion_slack_ns",p.congestion_slack_ns);
    p.qp_limit=cfg.value("qp_depth",128U);
    p.affinity_slack_ns=cfg.value("affinity_slack_ns",p.affinity_slack_ns);
    auto kind=candidate_policy==1?mr::Policy::LargestRemainder:candidate_policy==2?mr::Policy::EarliestFinish:mr::Policy::ByteDeficit;
    policy_owners.insert_or_assign(owner,std::make_unique<mr::Scheduler>(kind,p));
}
inline mr::Assignment candidateAllocate(mooncake::tent::DeviceSelector* owner,
        const std::vector<mr::Candidate>& candidates,const std::vector<uint64_t>& bytes,
        bool probe,uint64_t group) {
    auto begin=std::chrono::steady_clock::now();
    auto out=candidate_policy==1?mr::allocateLargestRemainder(candidates,bytes,probe):policy_owners.at(owner)->allocate(candidates,bytes,now_ns,group);
    policy_ns.fetch_add(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-begin).count(),std::memory_order_relaxed);policy_calls.fetch_add(1,std::memory_order_relaxed);
    policyDecision(candidates,out);return out;
}
inline void policyPosted(mooncake::tent::DeviceSelector* owner,int rail,uint64_t bytes,uint64_t post) {
    if(candidate_policy>1)policy_owners.at(owner)->posted(rail,bytes,post);
}
inline void policyCompleted(mooncake::tent::DeviceSelector* owner,int rail,uint64_t bytes,uint64_t post,uint64_t now,bool posted=true,bool success=true) {
    if(candidate_policy>1)policy_owners.at(owner)->completed(rail,bytes,post,now,posted,success);
}
inline nlohmann::json policyStats() {
    nlohmann::json result=nlohmann::json::array();
    for(const auto& entry:policy_owners){auto stats=entry.second->stats();for(size_t i=0;i<stats.size();++i)if(stats[i].capacity>0){auto& s=stats[i];result.push_back({{"rail",i},{"reserved",s.reserved_bytes},{"posted",s.posted},{"posted_bytes",s.posted_bytes},{"capacity_Bps",s.capacity},{"latency_ns",s.latency_ns},{"capacity_updates",s.updates}});}}
    return result;
}
}
