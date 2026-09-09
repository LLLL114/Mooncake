// Bounded real RC RDMA WRITE loopback capability and data-integrity probe.
#include <infiniband/verbs.h>
#include <chrono>
#include <cstring>
#include <cstdlib>
#include <iostream>
#include <vector>
#include <stdexcept>
#include <cerrno>
void need(bool ok,const char* what){if(!ok)throw std::runtime_error(std::string(what)+": "+strerror(errno));}
int main(int argc,char** argv){
 ibv_context* ctx=nullptr;ibv_pd* pd=nullptr;ibv_cq* cq=nullptr;ibv_qp* q[2]={};ibv_mr* mr[2]={};void* mem[2]={};int rc=1;
 try {
  need(argc==3,"usage: probe device bytes");size_t n=std::stoull(argv[2]);need(n && n<=1048576,"bounded bytes");
  int num=0;auto list=ibv_get_device_list(&num);need(list,"device list");
  for(int i=0;i<num;++i)if(!strcmp(ibv_get_device_name(list[i]),argv[1]))ctx=ibv_open_device(list[i]);
  ibv_free_device_list(list);need(ctx,"open device");ibv_device_attr da{};need(!ibv_query_device(ctx,&da),"query device");
  std::cout<<"DEVICE "<<argv[1]<<" transport "<<ctx->device->transport_type<<std::endl;
  pd=ibv_alloc_pd(ctx);need(pd,"alloc pd");cq=ibv_create_cq(ctx,64,nullptr,nullptr,0);need(cq,"create cq");
  for(int i=0;i<2;++i) {
   need(!posix_memalign(&mem[i],4096,n),"aligned allocation");memset(mem[i],i?0:0x5a,n);
   mr[i]=ibv_reg_mr(pd,mem[i],n,IBV_ACCESS_LOCAL_WRITE|IBV_ACCESS_REMOTE_WRITE|IBV_ACCESS_REMOTE_READ);need(mr[i],"register mr");
   ibv_qp_init_attr init{};init.send_cq=cq;init.recv_cq=cq;init.qp_type=IBV_QPT_RC;init.cap.max_send_wr=16;init.cap.max_recv_wr=16;init.cap.max_send_sge=1;init.cap.max_recv_sge=1;
   q[i]=ibv_create_qp(pd,&init);need(q[i],"create qp");
   ibv_qp_attr a{};a.qp_state=IBV_QPS_INIT;a.port_num=1;a.pkey_index=0;a.qp_access_flags=IBV_ACCESS_REMOTE_WRITE|IBV_ACCESS_REMOTE_READ;
   need(!ibv_modify_qp(q[i],&a,IBV_QP_STATE|IBV_QP_PKEY_INDEX|IBV_QP_PORT|IBV_QP_ACCESS_FLAGS),"QP INIT");
  }
  ibv_port_attr port{};need(!ibv_query_port(ctx,1,&port),"query port");ibv_gid gid{};need(!ibv_query_gid(ctx,1,0,&gid),"query gid");
  for(int i=0;i<2;++i) {
   ibv_qp_attr a{};a.qp_state=IBV_QPS_RTR;a.path_mtu=port.active_mtu;a.dest_qp_num=q[1-i]->qp_num;a.rq_psn=0;a.max_dest_rd_atomic=1;a.min_rnr_timer=12;
   a.ah_attr.port_num=1;a.ah_attr.dlid=port.lid;a.ah_attr.is_global=1;a.ah_attr.grh.dgid=gid;a.ah_attr.grh.sgid_index=0;a.ah_attr.grh.hop_limit=1;
   need(!ibv_modify_qp(q[i],&a,IBV_QP_STATE|IBV_QP_AV|IBV_QP_PATH_MTU|IBV_QP_DEST_QPN|IBV_QP_RQ_PSN|IBV_QP_MAX_DEST_RD_ATOMIC|IBV_QP_MIN_RNR_TIMER),"QP RTR direct-verbs path");
   a={};a.qp_state=IBV_QPS_RTS;a.timeout=14;a.retry_cnt=2;a.rnr_retry=2;a.sq_psn=0;a.max_rd_atomic=1;
   need(!ibv_modify_qp(q[i],&a,IBV_QP_STATE|IBV_QP_TIMEOUT|IBV_QP_RETRY_CNT|IBV_QP_RNR_RETRY|IBV_QP_SQ_PSN|IBV_QP_MAX_QP_RD_ATOMIC),"QP RTS");
  }
  ibv_sge sg{};sg.addr=reinterpret_cast<uintptr_t>(mem[0]);sg.length=n;sg.lkey=mr[0]->lkey;
  ibv_send_wr wr{},*bad=nullptr;wr.wr_id=1;wr.sg_list=&sg;wr.num_sge=1;wr.opcode=IBV_WR_RDMA_WRITE;wr.send_flags=IBV_SEND_SIGNALED;wr.wr.rdma.remote_addr=reinterpret_cast<uintptr_t>(mem[1]);wr.wr.rdma.rkey=mr[1]->rkey;
  auto begin=std::chrono::steady_clock::now();need(!ibv_post_send(q[0],&wr,&bad),"post send");ibv_wc wc{};
  while(true){int n=ibv_poll_cq(cq,1,&wc);need(n>=0,"poll cq");if(n)break;need(std::chrono::steady_clock::now()-begin<std::chrono::seconds(3),"CQ deadline");}
  need(wc.status==IBV_WC_SUCCESS,ibv_wc_status_str(wc.status));need(!memcmp(mem[0],mem[1],n),"payload mismatch");
  std::cout<<"PASS bytes="<<n<<" elapsed_us="<<std::chrono::duration<double,std::micro>(std::chrono::steady_clock::now()-begin).count()<<std::endl;rc=0;
 }catch(const std::exception& e){std::cerr<<"PROBE_FAILED "<<e.what()<<std::endl;}
 for(int i=0;i<2;++i)if(q[i])ibv_destroy_qp(q[i]);for(int i=0;i<2;++i){if(mr[i])ibv_dereg_mr(mr[i]);free(mem[i]);}if(cq)ibv_destroy_cq(cq);if(pd)ibv_dealloc_pd(pd);if(ctx)ibv_close_device(ctx);return rc;
}
