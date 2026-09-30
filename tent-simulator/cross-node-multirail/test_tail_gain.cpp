#include "stable_quota.h"
#include <cassert>
#include <cmath>
#include <iostream>

int main() {
    using tent_sq::Controller;
    Controller s; s.init(738721792.,711808341.3333334);
    for (unsigned i=0;i<1000;++i) {
        const auto p=s.integer(1+i*10000000ULL,i%2?0:100000000,i%2?100000000:0,false);
        assert(p.first==8 && p.weight==.5 && p.offset==0);
    }
    s.init(1e9,1e9);
    uint64_t last_change=0; unsigned previous=8;
    for (unsigned i=0;i<=100;++i) {
        const uint64_t now=1+i*10000000ULL;
        const auto p=s.integer(now,16777216,0,true);
        assert(p.first<=16 && p.weight==double(p.first)/16);
        if (i<6) assert(p.first==8); // Three complete 20ms windows are required.
        if (p.first!=previous) {
            assert(std::abs(int(p.first)-int(previous))<=2);
            assert(!last_change || now-last_change>=100000000);
            last_change=now; previous=p.first;
        }
    }
    assert(previous==0); // Endpoint becomes usable after several justified steps.
    assert(s.integer(3000000001,0,0,true).first==0); // Idle does not force a reset.
    for (unsigned i=1;i<=100;++i) s.integer(3000000001+i*10000000ULL,0,0,true);
    assert(s.held==8);
    s.init(1e6,1e9);
    for (unsigned i=0;i<100;++i) {
        // All-to-slow-rail endpoint looks better, but the permitted 2-slice step
        // does not beat the margin: never use the full target's gain as evidence.
        assert(s.integer(1+i*20000000ULL,0,2147483648ULL,true).first==0);
    }
    s.init(1e9,1e9);
    for (unsigned i=0;i<100;++i) {
        const auto p=s.integer(1+i*20000000ULL,i%2?0:16777216,i%2?16777216:0,true);
        assert(p.first==8); // Alternating evidence must not obtain three confirmations.
    }
    std::cout<<"TAIL_GAIN_TEST_OK\n";
}
