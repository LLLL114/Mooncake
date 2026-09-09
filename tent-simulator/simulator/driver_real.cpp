// Actual RDMA-CM endpoints and WRITE/CQ data path, with real DeviceSelector.
#include "driver_d_shared.inc"
#include <rdma/rdma_cma.h>
#include <rdma/rdma_verbs.h>
#include <arpa/inet.h>
#include <thread>
#include <cstring>

namespace {
void sysok(bool value,const char* msg){require(value,std::string(msg)+": "+strerror(errno));}
uint64_t clockNs(){return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
struct Endpoint {
 rdma_cm_id *listener=nullptr,*server=nullptr,*client=nullptr;
 ibv_mr *source_mr=nullptr,*target_mr=nullptr;void *source=nullptr,*target=nullptr;bool own_source=true;
 ~Endpoint(){if(client)rdma_destroy_qp(client);if(server)rdma_destroy_qp(server);if(source_mr)ibv_dereg_mr(source_mr);if(target_mr)ibv_dereg_mr(target_mr);if(client)rdma_destroy_ep(client);if(server)rdma_destroy_ep(server);if(listener)rdma_destroy_ep(listener);if(own_source)free(source);free(target);}
 void connect(const char* ip,int port,size_t bytes,void* shared_source=nullptr) {
  ibv_qp_init_attr qp{};qp.qp_type=IBV_QPT_RC;qp.cap.max_send_wr=256;qp.cap.max_recv_wr=16;qp.cap.max_send_sge=1;qp.cap.max_recv_sge=1;qp.sq_sig_all=1;
  rdma_addrinfo hints{},*ai=nullptr;hints.ai_flags=RAI_PASSIVE;hints.ai_port_space=RDMA_PS_TCP;hints.ai_qp_type=IBV_QPT_RC;
  auto service=std::to_string(port);sysok(!rdma_getaddrinfo(ip,service.data(),&hints,&ai),"server address");sysok(!rdma_create_ep(&listener,ai,nullptr,&qp),"listener");rdma_freeaddrinfo(ai);sysok(!rdma_listen(listener,1),"listen");
  std::string failure;
  std::thread accepting([&]{try {sysok(!rdma_get_request(listener,&server),"server request");sysok(!rdma_accept(server,nullptr),"accept");}catch(const std::exception& e){failure=e.what();}});
  try {
   hints.ai_flags=0;sockaddr_in src{};src.sin_family=AF_INET;inet_pton(AF_INET,ip,&src.sin_addr);hints.ai_src_addr=reinterpret_cast<sockaddr*>(&src);hints.ai_src_len=sizeof(src);
   sysok(!rdma_getaddrinfo(ip,service.data(),&hints,&ai),"client address");sysok(!rdma_create_ep(&client,ai,nullptr,&qp),"client endpoint");rdma_freeaddrinfo(ai);sysok(!rdma_connect(client,nullptr),"connect");
  }catch(const std::exception& e){std::cerr<<"connection setup failed: "<<e.what()<<std::endl;std::_Exit(2);}
  accepting.join();require(failure.empty(),failure);
  own_source=!shared_source;source=shared_source;
  if(own_source)sysok(!posix_memalign(&source,4096,bytes),"allocate source");
  sysok(!posix_memalign(&target,4096,bytes),"allocate target");memset(source,0x5a,bytes);memset(target,0,bytes);
  source_mr=ibv_reg_mr(client->pd,source,bytes,IBV_ACCESS_LOCAL_WRITE);target_mr=ibv_reg_mr(server->pd,target,bytes,IBV_ACCESS_LOCAL_WRITE|IBV_ACCESS_REMOTE_WRITE);
  sysok(source_mr && target_mr,"register payload");
 }
};
}
int main(int argc,char** argv){
 try {
  require(argc==3,"usage: real config output");std::ifstream in(argv[1]);in>>config;
  const uint64_t n=config.value("requests",4096ULL),length=config.value("request_bytes",1048576ULL),gap=config.value("arrival_interval_ns",0ULL),slots=64;
  const int mask=config.value("rail_mask",3);require(n>=512 && n<=50000 && (mask>=1 && mask<=3),"bounded real run");
  auto split=productionSplit(length);require(split.count<=32 && length%split.bytes==0,"real test uses full slices only");uint64_t count=split.count;
  Endpoint endpoints[2];for(int r=0;r<2;++r)if(mask&(1<<r))endpoints[r].connect(r?"10.0.1.244":"10.0.1.243",18621+r,slots*length,r==1 && (mask&1)?endpoints[0].source:nullptr);
  auto sel=selector(2,config.value("smart",true));live_selector=sel.get();
  SimpleRandom::Get()=SimpleRandom(config.value("seed",1U));experiment::allocation_mode=config.value("allocation_mode",std::string("original"))=="remainder"?1:0;
  experiment::aggregate=true;metric_start=0;metric_stop=UINT64_MAX;
  std::vector<std::vector<int>> routes(n);std::vector<std::vector<uint64_t>> charges(n),post_ns(n,std::vector<uint64_t>(count));
  std::vector<uint64_t> remaining(n,count),submitted(n),finished(n),planned(n);
  std::array<std::deque<std::pair<uint64_t,uint64_t>>,2> waiting;
  std::array<uint64_t,2> posted{},writes{},done_bytes{},max_posted{};
  std::vector<bool> busy(slots,false);std::array<std::vector<bool>,2> touched={std::vector<bool>(slots*count),std::vector<bool>(slots*count)};
  uint64_t next=0,done=0,wc_count=0,begin=clockNs(),end=0,max_late=0;double allocation_tv=0,last_share=0;
  Json trace=Json::array();std::vector<uint64_t> slice_completions;
  while(done<n) {
   uint64_t now=clockNs();require(now-begin<30000000000ULL,"real run deadline exceeded");
   for(int rail=0;rail<2;++rail)if(mask&(1<<rail)) {
    ibv_wc wc[32];int got=ibv_poll_cq(endpoints[rail].client->send_cq,32,wc);sysok(got>=0,"poll CQ");uint64_t polltime=clockNs();
    for(int j=0;j<got;++j) {
     require(wc[j].status==IBV_WC_SUCCESS,ibv_wc_status_str(wc[j].status));auto req=wc[j].wr_id>>6,s=wc[j].wr_id&63;
     require(req<next && s<count && routes[req][s]==rail && posted[rail]>0 && remaining[req]>0,"WC identity/conservation");
     --posted[rail];++wc_count;done_bytes[rail]+=split.bytes;
     ok(sel->release(rail,charges[req][s],(polltime-post_ns[req][s])/1e9));
     if(--remaining[req]==0){finished[req]=polltime-begin;busy[req%slots]=false;++done;}
    }
   }
   now=clockNs();
   if(next<n && !busy[next%slots] && (!gap || now-begin>=next*gap)) {
    auto req=next++;busy[req%slots]=true;submitted[req]=now-begin;planned[req]=gap?req*gap:submitted[req];max_late=std::max(max_late,submitted[req]-planned[req]);
    experiment::now_ns=now-begin;current_request=req;
    if(split.aggregate)ok(sel->allocate(length,count,split.bytes,"cpu:0",routes[req],PRIO_HIGH,mask,&charges[req]));
    else for(uint64_t s=0;s<count;++s){std::vector<int> rr;std::vector<uint64_t> cc;ok(sel->allocate(split.bytes,1,split.bytes,"cpu:0",rr,PRIO_HIGH,mask,&cc));routes[req].push_back(rr[0]);charges[req].push_back(cc[0]);}
    double share=double(std::count(routes[req].begin(),routes[req].end(),0))/count;if(req>=256){if(req>256)allocation_tv+=std::abs(share-last_share);last_share=share;}
    if(req>=256 && req<512)trace.push_back({{"request",req},{"share0",share},{"weight0",d_first[0]}});
    for(uint64_t s=0;s<count;++s)waiting[routes[req][s]].push_back({req,s});
   }
   for(int rail=0;rail<2;++rail)while(!waiting[rail].empty() && posted[rail]<128) {
    auto [req,s]=waiting[rail].front();waiting[rail].pop_front();auto& e=endpoints[rail];size_t offset=(req%slots)*length+s*split.bytes;
    ibv_sge sg{};sg.addr=reinterpret_cast<uint64_t>(e.source)+offset;sg.length=split.bytes;sg.lkey=e.source_mr->lkey;
    ibv_send_wr wr{},*bad=nullptr;wr.wr_id=(req<<6)|s;wr.sg_list=&sg;wr.num_sge=1;wr.opcode=IBV_WR_RDMA_WRITE;wr.send_flags=IBV_SEND_SIGNALED;wr.wr.rdma.remote_addr=reinterpret_cast<uint64_t>(e.target)+offset;wr.wr.rdma.rkey=e.target_mr->rkey;
    post_ns[req][s]=clockNs();sysok(!ibv_post_send(e.client->qp,&wr,&bad),"post WRITE");++posted[rail];++writes[rail];max_posted[rail]=std::max(max_posted[rail],posted[rail]);touched[rail][(req%slots)*count+s]=true;
   }
  }
  end=clockNs()-begin;assertDrained(*sel);require(wc_count==n*count && done_bytes[0]+done_bytes[1]==n*length,"real completion/byte conservation");
  uint64_t verified=0;for(int r=0;r<2;++r)if(mask&(1<<r))for(uint64_t s=0;s<slots*count;++s)if(touched[r][s]) {
   auto ptr=static_cast<unsigned char*>(endpoints[r].target)+s*split.bytes;for(uint64_t j=0;j<split.bytes;++j)require(ptr[j]==0x5a,"remote payload mismatch");verified+=split.bytes;
  }
  std::vector<uint64_t> scheduled(n),service(n);for(uint64_t r=0;r<n;++r){scheduled[r]=finished[r]-planned[r];service[r]=finished[r]-submitted[r];}
  Json devices=Json::array();for(int r=0;r<2;++r)if(mask&(1<<r))devices.push_back({{"rail",r},{"source_nic",ibv_get_device_name(endpoints[r].client->verbs->device)},{"target_nic",ibv_get_device_name(endpoints[r].server->verbs->device)}});
  Json result={{"config",config},{"source","actual_RDMA_WRITE_CQ_same_host_real_DeviceSelector"},{"devices",devices},{"elapsed_ns",end},{"request_scheduled_latency_ns",scheduled},{"request_service_latency_ns",service},{"submitted_ns",submitted},{"finished_ns",finished},{"max_injection_lateness_ns",max_late},{"writes",writes},{"completion_count",wc_count},{"completion_bytes",done_bytes},{"verified_final_slot_bytes",verified},{"allocation_step",allocation_tv/(n-257)},{"trace",trace},{"max_posted",max_posted},{"final_stats",stats(*sel)},{"verification_scope","all signaled WR completions plus final contents of every written ring-slot slice; not unique payload verification for every overwritten request"}};
  std::ofstream out(argv[2]);out<<result.dump()<<'\n';return 0;
 }catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
}
