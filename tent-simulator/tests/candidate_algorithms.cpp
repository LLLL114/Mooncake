#include "rail_scheduler.h"
#include <iostream>
#include <thread>
#include <atomic>
#include <cmath>
#include <algorithm>
#include <numeric>
using namespace mooncake::tent::multirail;
void check(bool value,const char* name){if(!value)throw std::runtime_error(name);std::cout<<"PASS "<<name<<'\n';}
int main(){try {
 const std::vector<uint64_t> slices(16,65536);
 auto equal=std::vector<Candidate>{{0,1,12.5e9,1},{1,1,12.5e9,1}};
 auto unequal=equal;unequal[0].score=1/.49;unequal[1].score=1/.51;
 auto a=allocateLargestRemainder(unequal,slices);check(std::count(a.ids.begin(),a.ids.end(),0)==8,"largest_remainder_49_51");
 a=allocateLargestRemainder(equal,std::vector<uint64_t>(17,65536));check(a.ids.size()==17 && std::count(a.ids.begin(),a.ids.end(),0)==9,"odd_slice_deterministic_tie");
 auto tail=slices;tail.back()=16384;a=allocateLargestRemainder(equal,tail);check(a.bytes==tail && std::accumulate(a.bytes.begin(),a.bytes.end(),0ULL)==999424,"actual_tail_bytes");
 auto many=equal;many.push_back({2,1,12.5e9,1});a=allocateLargestRemainder(many,{65536,65536});check(a.ids==std::vector<int>({0,1}),"more_rails_than_slices");
 a=allocateLargestRemainder(unequal,slices,true);check(std::count(a.ids.begin(),a.ids.end(),0)==8,"legacy_probe_preserved");
 for(auto policy:{Policy::LargestRemainder,Policy::EarliestFinish,Policy::ByteDeficit}) {
  Scheduler scheduler(policy);auto out=scheduler.allocate(equal,tail,0);auto stats=scheduler.stats();
  check(stats[0].reserved_bytes+stats[1].reserved_bytes==999424,"reservation_byte_conservation");
  for(size_t i=0;i<out.ids.size();++i)scheduler.completed(out.ids[i],out.bytes[i],0,0,false,false);
  stats=scheduler.stats();check(stats[0].reserved_bytes+stats[1].reserved_bytes==0,"unposted_cancel_drains");
  out=scheduler.allocate({equal[1]},slices,100);check(std::all_of(out.ids.begin(),out.ids.end(),[](int i){return i==1;}),"ineligible_rail_excluded");
  for(size_t i=0;i<out.ids.size();++i){scheduler.posted(out.ids[i],out.bytes[i],100);scheduler.completed(out.ids[i],out.bytes[i],100,200,true,false);}
  check(scheduler.stats()[1].posted==0 && scheduler.stats()[1].reserved_bytes==0,"failed_posted_completions_drain");
  bool rejected=false;try{scheduler.allocate({},slices,0);}catch(const std::invalid_argument&){rejected=true;}check(rejected,"all_unavailable_returns_error");
 }
 Scheduler finish(Policy::EarliestFinish);auto busy=finish.allocate({equal[0]},slices,0);a=finish.allocate(equal,{65536},1);check(a.ids[0]==1,"reserved_bytes_are_visible");
 for(size_t i=0;i<busy.ids.size();++i)finish.completed(0,busy.bytes[i],0,0,false,false);finish.completed(1,65536,0,0,false,false);
 Scheduler qp(Policy::EarliestFinish);auto q=qp.allocate({equal[0]},std::vector<uint64_t>(128,1),0);for(auto bytes:q.bytes)qp.posted(0,bytes,1);
 a=qp.allocate(equal,{1},2);check(a.ids[0]==1,"qp_pressure_guard");for(auto bytes:q.bytes)qp.completed(0,bytes,1,2,true,false);qp.completed(1,1,0,0,false,false);
 // Analytical FIFO completion spacing is 10us per 64KiB, independent of an
 // initial 50us delay. Long busy stream lets the capacity EWMA converge.
 Parameters p;p.capacity_tau_ns=100000;p.sample_min_ns=20000;p.sample_min_bytes=262144;
 Scheduler measured(Policy::EarliestFinish,p);auto stream=measured.allocate({equal[0]},std::vector<uint64_t>(4096,65536),0);
 for(auto bytes:stream.bytes)measured.posted(0,bytes,0);
 for(size_t i=0;i<stream.ids.size();++i)measured.completed(0,65536,0,50000+(i+1)*10000,true,true);
 check(std::abs(measured.stats()[0].capacity-6.5536e9)/6.5536e9<.001,"busy_completion_rate_estimator");
 check(measured.stats()[0].updates>100,"estimator_received_samples");
 Scheduler idle(Policy::EarliestFinish);for(int i=0;i<100;++i){auto one=idle.allocate({equal[0]},{65536},i*1000000ULL);idle.posted(0,65536,i*1000000ULL);idle.completed(0,65536,i*1000000ULL,i*1000000ULL+60000,true,true);}
 check(idle.stats()[0].capacity==12.5e9,"idle_rate_does_not_become_capacity");
 Parameters fair;fair.probe_interval_ns=0;fair.congestion_slack_ns=1000000000;
 Scheduler deficit(Policy::ByteDeficit,fair);uint64_t c0=0,c1=0;
 for(int k=0;k<1001;++k){auto one=deficit.allocate(equal,{65536},k*100000ULL);(one.ids[0]==0?c0:c1)+=65536;deficit.completed(one.ids[0],65536,0,0,false,false);}
 check(std::llabs(static_cast<long long>(c0)-static_cast<long long>(c1))<=65536,"byte_debt_long_term_fairness");
 Scheduler shared(Policy::EarliestFinish);std::vector<std::thread> threads;
 for(int t=0;t<8;++t)threads.emplace_back([&]{for(int k=0;k<1000;++k){auto one=shared.allocate(equal,{65536},0);shared.posted(one.ids[0],65536,0);shared.completed(one.ids[0],65536,0,10000,true,true);}});
 for(auto& t:threads)t.join();auto state=shared.stats();check(state[0].reserved_bytes==0 && state[1].reserved_bytes==0 && state[0].posted==0 && state[1].posted==0,"concurrent_allocation_and_completion");
 Scheduler transaction(Policy::EarliestFinish);auto pending=transaction.allocate(equal,{65536},0);int id=pending.ids[0];transaction.posted(id,65536,100);
 bool bad=false;try{transaction.completed(id,65536,100,99,true,true);}catch(const std::invalid_argument&){bad=true;}
 check(bad && transaction.stats()[id].reserved_bytes==65536 && transaction.stats()[id].posted==1,"invalid_completion_is_nonmutating");transaction.completed(id,65536,100,200,true,true);
 Scheduler weighted(Policy::ByteDeficit,fair);auto c=equal;c[0].nominal_bytes_per_second=25e9;uint64_t bytes0=0;
 for(int k=0;k<900;++k){auto one=weighted.allocate(c,{65536},k*100000ULL);if(one.ids[0]==0)bytes0+=65536;weighted.completed(one.ids[0],65536,0,0,false,false);}
 check(std::llabs(static_cast<long long>(bytes0)-600LL*65536)<=65536,"weighted_byte_fairness");
 Scheduler coordinated(Policy::EarliestFinish);std::atomic<int> ready{0};std::vector<int> choices(8);threads.clear();
 for(int t=0;t<8;++t)threads.emplace_back([&,t]{auto one=coordinated.allocate(equal,{65536},0);choices[t]=one.ids[0];++ready;while(ready.load()!=8)std::this_thread::yield();coordinated.completed(one.ids[0],65536,0,0,false,false);});
 for(auto& t:threads)t.join();check(std::count(choices.begin(),choices.end(),0)==4,"concurrent_reservations_avoid_herd");
 Scheduler bounded_affinity(Policy::EarliestFinish);auto numa=equal;numa[1].numa_penalty=5;
 auto balance=bounded_affinity.allocate(numa,slices,0);check(std::count(balance.ids.begin(),balance.ids.end(),0)==8,"affinity_does_not_multiply_existing_queue");
 for(size_t i=0;i<balance.ids.size();++i)bounded_affinity.completed(balance.ids[i],balance.bytes[i],0,0,false,false);
 std::cout<<"ALGORITHM_TESTS_OK\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
