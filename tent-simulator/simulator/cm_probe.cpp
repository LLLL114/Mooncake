// Same-host RDMA-CM RC WRITE probe. External runner bounds every blocking call.
#include <rdma/rdma_cma.h>
#include <rdma/rdma_verbs.h>
#include <arpa/inet.h>
#include <cstring>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <chrono>
void need(bool ok,const char* s){if(!ok)throw std::runtime_error(std::string(s)+": "+strerror(errno));}
struct Descriptor {uint64_t address;uint32_t rkey,length;};
int main(int argc,char** argv){
 rdma_cm_id *listen=nullptr,*id=nullptr;rdma_addrinfo* ai=nullptr;ibv_mr *payload_mr=nullptr,*msg_mr=nullptr,*notify_mr=nullptr;void* payload=nullptr;int rc=1;
 try {
  need(argc==5,"usage: cm_probe server|client destination_ip port source_ip");bool server=std::string(argv[1])=="server";
  rdma_addrinfo hints{};hints.ai_port_space=RDMA_PS_TCP;hints.ai_qp_type=IBV_QPT_RC;if(server)hints.ai_flags=RAI_PASSIVE;
  sockaddr_in src{};src.sin_family=AF_INET;need(inet_pton(AF_INET,argv[4],&src.sin_addr)==1,"source address");
  if(!server){hints.ai_src_addr=reinterpret_cast<sockaddr*>(&src);hints.ai_src_len=sizeof(src);}
  need(!rdma_getaddrinfo(argv[2],argv[3],&hints,&ai),"getaddrinfo");
  ibv_qp_init_attr attr{};attr.qp_type=IBV_QPT_RC;attr.cap.max_send_wr=16;attr.cap.max_recv_wr=16;attr.cap.max_send_sge=1;attr.cap.max_recv_sge=1;attr.sq_sig_all=1;
  if(server){need(!rdma_create_ep(&listen,ai,nullptr,&attr),"create listener");need(!rdma_listen(listen,1),"listen");std::cout<<"LISTEN_READY"<<std::endl;need(!rdma_get_request(listen,&id),"get request");}
  else need(!rdma_create_ep(&id,ai,nullptr,&attr),"create client / resolve address and route");
  std::cout<<"DEVICE "<<ibv_get_device_name(id->verbs->device)<<std::endl;
  constexpr size_t bytes=65536;need(!posix_memalign(&payload,4096,bytes),"allocate");memset(payload,server?0:0x5a,bytes);
  payload_mr=ibv_reg_mr(id->pd,payload,bytes,IBV_ACCESS_LOCAL_WRITE|IBV_ACCESS_REMOTE_WRITE);need(payload_mr,"register payload");
  Descriptor msg{};unsigned char notify=0x77;
  msg_mr=rdma_reg_msgs(id,&msg,sizeof(msg));notify_mr=rdma_reg_msgs(id,&notify,1);need(msg_mr && notify_mr,"register messages");
  ibv_wc wc{};
  if(server){
   need(!rdma_post_recv(id,nullptr,&notify,1,notify_mr),"post notify receive");need(!rdma_accept(id,nullptr),"accept");
   msg={reinterpret_cast<uint64_t>(payload),payload_mr->rkey,bytes};need(!rdma_post_send(id,nullptr,&msg,sizeof(msg),msg_mr,IBV_SEND_SIGNALED),"send descriptor");
   need(rdma_get_send_comp(id,&wc)>0 && wc.status==IBV_WC_SUCCESS,"descriptor completion");
   need(rdma_get_recv_comp(id,&wc)>0 && wc.status==IBV_WC_SUCCESS,"notify completion");
   need(notify==0x77,"notify value");for(size_t i=0;i<bytes;++i)need(static_cast<unsigned char*>(payload)[i]==0x5a,"payload mismatch");
   std::cout<<"PAYLOAD_PASS bytes="<<bytes<<std::endl;
  }else{
   need(!rdma_post_recv(id,nullptr,&msg,sizeof(msg),msg_mr),"post descriptor receive");need(!rdma_connect(id,nullptr),"connect");
   need(rdma_get_recv_comp(id,&wc)>0 && wc.status==IBV_WC_SUCCESS,"receive descriptor");need(msg.length==bytes,"length contract");
   auto begin=std::chrono::steady_clock::now();
   need(!rdma_post_write(id,nullptr,payload,bytes,payload_mr,IBV_SEND_SIGNALED,msg.address,msg.rkey),"post RDMA WRITE");
   need(rdma_get_send_comp(id,&wc)>0 && wc.status==IBV_WC_SUCCESS,"WRITE completion");
   std::cout<<"WRITE_CQ_PASS elapsed_us="<<std::chrono::duration<double,std::micro>(std::chrono::steady_clock::now()-begin).count()<<std::endl;
   need(!rdma_post_send(id,nullptr,&notify,1,notify_mr,IBV_SEND_SIGNALED),"send notify");need(rdma_get_send_comp(id,&wc)>0 && wc.status==IBV_WC_SUCCESS,"notify send completion");
  }
  rc=0;
 }catch(const std::exception& e){std::cerr<<"CM_PROBE_FAILED "<<e.what()<<std::endl;}
 if(payload_mr)ibv_dereg_mr(payload_mr);if(msg_mr)rdma_dereg_mr(msg_mr);if(notify_mr)rdma_dereg_mr(notify_mr);if(id)rdma_destroy_ep(id);if(listen)rdma_destroy_ep(listen);if(ai)rdma_freeaddrinfo(ai);free(payload);return rc;
}
