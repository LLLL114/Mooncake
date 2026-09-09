// Real selector calls on persistent OS threads; the wire/CQ remain simulated.
#include "concurrency.h"
#define main previousMain
#include "driver.cpp"
#undef main
#include <condition_variable>
#include <thread>
#include <mutex>

namespace {
struct Barrier {
    std::mutex m; std::condition_variable cv; unsigned count=0,total=1,epoch=0;
    void wait() { std::unique_lock<std::mutex> lock(m);auto e=epoch;
        if(++count==total) {count=0;++epoch;cv.notify_all();}
        else cv.wait(lock,[&]{return epoch!=e;}); }
} barrier;
thread_local double first_p=0;
thread_local bool first_candidate=true;
struct Job { DeviceSelector* selector=nullptr; uint64_t length=0; int id=0;
    std::vector<int> rails;std::vector<uint64_t> charges; double p=0;std::string error; };
struct Pool {
    std::mutex m;std::condition_variable ready,done;
    uint64_t epoch=0;unsigned left=0;bool stop=false;
    std::vector<Job> jobs;std::vector<bool> active;std::vector<std::thread> threads;
    Pool(int n,uint32_t seed):jobs(n),active(n,false) {
        for(int id=0;id<n;++id)threads.emplace_back([&,id,seed] {
            SimpleRandom::Get()=SimpleRandom(seed+id*1009U);
            uint64_t seen=0;
            while(true) {
                std::unique_lock<std::mutex> lock(m);
                ready.wait(lock,[&]{return stop || epoch!=seen;}); if(stop)return;
                seen=epoch;bool run=active[id];lock.unlock();if(!run)continue;
                auto& j=jobs[id];j.rails.clear();j.charges.clear();j.error.clear();first_candidate=true;
                try { auto split=productionSplit(j.length);
                    if(split.aggregate) ok(j.selector->allocate(j.length,split.count,split.bytes,"cpu:0",j.rails,PRIO_HIGH,~0ULL,&j.charges));
                    else {uint64_t offset=0;for(uint64_t s=0;s<split.count;++s) {
                        uint64_t bytes=std::min(split.bytes,j.length-offset);offset+=bytes;
                        std::vector<int> rr;std::vector<uint64_t> cc;
                        ok(j.selector->allocate(bytes,1,bytes,"cpu:0",rr,PRIO_HIGH,~0ULL,&cc));
                        j.rails.push_back(rr[0]);j.charges.push_back(cc[0]);
                    }}
                    j.p=first_p;
                } catch(const std::exception& e) {j.error=e.what();}
                lock.lock();if(--left==0)done.notify_one();
            }
        });
    }
    void run(const std::vector<int>& ids) {
        std::unique_lock<std::mutex> lock(m);std::fill(active.begin(),active.end(),false);
        for(int i:ids)active[i]=true;
        barrier.total=ids.size();left=ids.size();++epoch;ready.notify_all();done.wait(lock,[&]{return left==0;});
        for(int id:ids) require(jobs[id].error.empty(),jobs[id].error);
    }
    ~Pool() { {std::lock_guard<std::mutex> lock(m);stop=true;ready.notify_all();}for(auto& t:threads)t.join(); }
};
}
namespace experiment {
void parallelCandidates(const std::vector<DeviceSelector::Candidate>& candidates) {
    if(first_candidate) { double total=0,w0=0;
        for(const auto& c:candidates){double w=1/(c.score+1e-12);total+=w;if(c.dev_id==0)w0=w;}
        first_p=w0/total;first_candidate=false;
    }
    // Deliberately force a valid worst-case interleaving: all active calls have
    // read candidate state before any can charge their choice.
    barrier.wait();
}
}
int main(int argc,char** argv) {
 try {
    require(argc==3,"usage: concurrency config output");std::ifstream input(argv[1]);input>>config;
    int processes=config.value("processes",1),workers=config.value("workers",1),clients=processes*workers;
    bool shared=config.value("shared_view",false),serial=config.value("serialize",false),burst=config.value("burst",true);
    require(clients>0 && clients<=32,"invalid clients");
    uint64_t n=config.value("requests",4096ULL),warm=config.value("warmup_requests",1024ULL),length=config.value("request_bytes",1048576ULL);
    uint64_t gap=config.value("arrival_interval_ns",70000ULL),poll=1000,qp=128;
    require(n%clients==0 && warm%clients==0 && warm<n,"align burst boundaries");
    std::vector<std::unique_ptr<DeviceSelector>> selectors;
    for(int p=0;p<(shared?1:processes);++p)selectors.push_back(selector(2));
    Pool pool(clients,config.value("seed",1U));
    for(int c=0;c<clients;++c) {pool.jobs[c].selector=selectors[shared?0:c/workers].get();pool.jobs[c].length=length;}
    auto split=productionSplit(length);uint64_t count=split.count;
    struct Item {uint64_t req,index,bytes,charge,arrival,post;int rail,owner;};
    struct Done {uint64_t at,order;Item s;bool operator>(const Done& b)const{return std::tie(at,order)>std::tie(b.at,b.order);}};
    std::priority_queue<Done,std::vector<Done>,std::greater<Done>> cq;
    std::array<std::deque<Item>,2> wait;
    std::array<uint64_t,2> posted{},wire{},assigned{},completed{},measured{};
    std::vector<uint64_t> pending(n,count),lat(n),arrivals(n);
    uint64_t now=0,order=0,done_count=0;double step=0,wstep=0;uint64_t samples=0,switches=0;
    std::vector<double> last_share(clients),last_weight(clients);std::vector<bool> seen(clients,false);std::vector<int> last_first(clients,-1);
    Json decisions=Json::array();
    auto start=[&](int rail) {while(!wait[rail].empty() && posted[rail]<qp) {
        auto s=wait[rail].front();wait[rail].pop_front();s.post=now;++posted[rail];
        wire[rail]=std::max(wire[rail],now)+static_cast<uint64_t>(std::ceil(s.bytes/12.5));
        cq.push({((wire[rail]+1000+poll-1)/poll)*poll,order++,s});
    }};
    auto finish=[&] {auto d=cq.top();cq.pop();now=d.at;experiment::now_ns=now;auto s=d.s;
        require(posted[s.rail]>0 && pending[s.req]>0,"duplicate/underflow");--posted[s.rail];
        ok(selectors[s.owner]->release(s.rail,s.charge,(now-s.post)/1e9));
        completed[s.rail]+=s.bytes;if(now>=warm*gap && now<n*gap)measured[s.rail]+=s.bytes;
        if(--pending[s.req]==0)lat[s.req]=now-s.arrival;++done_count;start(s.rail);
    };
    const int batch=burst?clients:1;
    for(uint64_t req=0;req<n;req+=batch) {
        uint64_t arrival=req*gap;while(!cq.empty() && cq.top().at<=arrival)finish();
        now=arrival;experiment::now_ns=now;std::vector<int> ids;
        for(int k=0;k<batch;++k)ids.push_back((req+k)%clients);
        if(serial) for(int c:ids)pool.run({c});else pool.run(ids);
        for(int k=0;k<batch;++k) {
            auto r=req+k;auto c=ids[k];auto& job=pool.jobs[c];uint64_t offset=0,b0=0;
            arrivals[r]=arrival;require(job.rails.size()==count,"allocation count");
            for(uint64_t s=0;s<count;++s) {
                auto bytes=std::min(split.bytes,length-offset);offset+=bytes;int rail=job.rails[s];
                assigned[rail]+=bytes;if(rail==0)b0+=bytes;
                wait[rail].push_back({r,s,bytes,job.charges[s],arrival,0,rail,shared?0:c/workers});
            }
            double share=double(b0)/length;
            if(r>=warm) {
                if(seen[c]) {step+=std::abs(share-last_share[c]);wstep+=std::abs(job.p-last_weight[c]);++samples;
                    switches+=last_first[c]!=job.rails[0];}
                seen[c]=true;last_share[c]=share;last_weight[c]=job.p;last_first[c]=job.rails[0];
                if(r<warm+256)decisions.push_back({{"request",r},{"client",c},{"arrival_ns",arrival},{"share0",share},{"weight0",job.p}});
            }
        }
        // All requests in the synchronous burst enter one shared service/QP
        // budget. Staggered cases have the same total offered byte rate.
        start(0);start(1);
    }
    while(!cq.empty())finish();
    require(assigned==completed && done_count==n*count,"byte/completion conservation");
    for(auto& s:selectors)assertDrained(*s);for(auto p:pending)require(p==0,"unresolved");
    Json result={{"config",config},{"source","real_threads_simulated_service_logical_process_views"},
        {"request_latency_ns",lat},{"arrival_ns",arrivals},{"allocation_bytes",assigned},{"completion_bytes",completed},
        {"measurement_bytes",measured},{"virtual_end_ns",now},{"slices",done_count},
        {"client_allocation_step",step/std::max<uint64_t>(1,samples)},{"client_weight_step",wstep/std::max<uint64_t>(1,samples)},
        {"client_switches_per_10k",switches*10000.0/std::max<uint64_t>(1,samples)},
        {"decisions",decisions},{"quota_leaks",0},{"qp_budget_per_rail",qp}};
    std::ofstream output(argv[2]);output<<result.dump()<<'\n';return 0;
 } catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
}
