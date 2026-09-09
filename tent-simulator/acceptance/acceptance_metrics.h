#pragma once
#include <array>
#include <cmath>
#include <cstdint>
#include <vector>

namespace acceptance {
// Raw request records use seven little-endian 8-byte fields (A10 x86_64).
struct Record {
    uint64_t planned=0, submitted=0, finished=0, allocation_ns=0;
    double first_weight0=0, share0=0;
    uint64_t first_rail=0;
};
static_assert(sizeof(Record)==56,"unexpected trace layout");
struct Changes {
    uint64_t samples=0;
    double tv=0, previous=0;
    std::array<uint64_t,3> events{}, reversals{};
    std::array<int,3> direction{};
    void observe(double p) {
        constexpr double eps[3]={1e-6,.001,.01};
        if(samples++) {
            double delta=p-previous;tv+=std::abs(delta);
            for(int k=0;k<3;++k)if(std::abs(delta)>eps[k]) {
                ++events[k];int d=delta>0?1:-1;
                if(direction[k] && d!=direction[k])++reversals[k];
                direction[k]=d;
            }
        }
        previous=p;
    }
};
struct Collector {
    bool enabled=true;
    uint64_t start=0, stop=0;
    Changes total;
    std::vector<Changes> windows;
    std::vector<std::array<uint64_t,2>> done, assigned;
    Collector(uint64_t lo,uint64_t hi,bool on):enabled(on),start(lo),stop(hi),
        windows((hi-lo+249999999)/250000000),done((hi+49999999)/50000000),assigned(done.size()){}
    void decision(uint64_t time,double p) {
        if(enabled && time>=start && time<stop) {
            total.observe(p);windows[(time-start)/250000000].observe(p);
        }
    }
    void bytes(uint64_t time,int rail,uint64_t n,bool completion) {
        if(time<stop)(completion?done:assigned)[time/50000000][rail]+=n;
    }
};
inline Collector* active=nullptr;
inline void decision(uint64_t time,double p) {if(active)active->decision(time,p);}
}
