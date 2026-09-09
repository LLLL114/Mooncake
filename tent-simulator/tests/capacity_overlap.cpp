// A 100G service has 50us visibility delay and 85us request spacing. Posted
// work overlaps across requests, although each request's wire burst has idle
// time after it. Learning the injection rate here is a false capacity drop.
#include "rail_scheduler.h"
#include <queue>
#include <iostream>
using namespace mooncake::tent::multirail;
int main(){Parameters p;p.capacity_tau_ns=100000;Scheduler s(Policy::EarliestFinish,p);
 std::vector<Candidate> c{{0,1,12.5e9,1}};std::vector<uint64_t> bytes(8,65536);
 std::priority_queue<std::pair<uint64_t,uint64_t>,std::vector<std::pair<uint64_t,uint64_t>>,std::greater<std::pair<uint64_t,uint64_t>>> done;
 auto finish=[&]{auto e=done.top();done.pop();s.completed(0,65536,e.second,e.first,true,true);};
 for(uint64_t k=0;k<2000;++k){uint64_t t=k*85000;while(!done.empty() && done.top().first<=t)finish();s.allocate(c,bytes,t);for(int i=0;i<8;++i){s.posted(0,65536,t);done.push({t+50000+(i+1)*5243,t});}}
 while(!done.empty())finish();auto r=s.stats()[0];std::cout<<r.capacity<<" "<<r.updates<<" "<<r.reserved_bytes<<" "<<r.posted<<"\n";
 return 0;
}
