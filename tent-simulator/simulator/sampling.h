#pragma once
#include "hooks.h"
#include <array>
#include <cmath>
#include <limits>

namespace experiment {
struct SampleMeter {
    uint64_t seen=0, accepted=0, same_timestamp=0, last_seen=UINT64_MAX;
    uint64_t last_update=0, min_dt=UINT64_MAX, max_dt=0;
    long double dt_sum=0, dt_sum2=0, raw_sum=0, accepted_sum=0, alpha_sum=0;
};
inline std::array<SampleMeter,8> meters{};
inline uint64_t sample_interval=0, sample_tau=0, sample_from=0, sample_to=UINT64_MAX;
inline bool sample_tail=false, batch_tail=true;
void configureSampling(const nlohmann::json& cfg) {
    sample_interval=cfg.value("update_interval_ns",0ULL);
    sample_tau=cfg.value("time_constant_ns",0ULL);
    sample_tail=cfg.value("sample_position",std::string("head"))=="tail";
    if(!cfg.value("temporal_estimator",false)) {
        sample_from=cfg.value("warmup_requests",0ULL)*cfg.value("arrival_interval_ns",50000ULL);
        sample_to=cfg.value("requests",1000ULL)*cfg.value("arrival_interval_ns",50000ULL);
    }
}
// Called only after real releaseInflight and only for successful samples.
// A zero interval with fixed alpha is exactly the original per-completion rule.
bool sampleGate(int device, double observed_bps, double& alpha) {
    auto& m=meters.at(device);
    const bool measured=now_ns>=sample_from && now_ns<sample_to;
    if(measured) {
        ++m.seen;m.raw_sum+=observed_bps/1e9;
        if(now_ns==m.last_seen)++m.same_timestamp;
        m.last_seen=now_ns;
    }
    const uint64_t dt=now_ns-m.last_update;
    if((sample_tail && !batch_tail) || dt<sample_interval || (sample_tau && dt==0))return false;
    if(sample_tau)alpha=std::exp(-static_cast<double>(dt)/sample_tau);
    m.last_update=now_ns;
    if(measured) {
        ++m.accepted;m.accepted_sum+=observed_bps/1e9;m.alpha_sum+=alpha;
        m.dt_sum+=dt;m.dt_sum2+=static_cast<long double>(dt)*dt;
        m.min_dt=std::min(m.min_dt,dt);m.max_dt=std::max(m.max_dt,dt);
    }
    return true;
}
nlohmann::json samplingStats(int count) {
    nlohmann::json rows=nlohmann::json::array();
    for(int i=0;i<count;++i) {
        const auto& m=meters.at(i);
        double mean=m.accepted ? double(m.dt_sum/m.accepted):0;
        double variance=m.accepted ? std::max(0.0,double(m.dt_sum2/m.accepted)-mean*mean):0;
        rows.push_back({{"rail",i},{"seen",m.seen},{"accepted",m.accepted},
            {"same_timestamp_samples",m.same_timestamp},
            {"min_interval_ns",m.accepted?m.min_dt:0},{"max_interval_ns",m.max_dt},
            {"mean_interval_ns",mean},{"interval_cv",mean?std::sqrt(variance)/mean:0},
            {"raw_sample_mean_GBps",m.seen?double(m.raw_sum/m.seen):0},
            {"accepted_sample_mean_GBps",m.accepted?double(m.accepted_sum/m.accepted):0},
            {"mean_alpha",m.accepted?double(m.alpha_sum/m.accepted):0}});
    }
    return rows;
}
}
