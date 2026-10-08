#!/usr/bin/env python3
"""Reuse the verified control/payload protocol, with an explicit freshly built TENT C library."""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import sys
import receiver_extended as protocol


class Engine:
    def __init__(self,library):
        self.lib=C.CDLL(str(library),mode=C.RTLD_GLOBAL);self.buffers={}
        for name,result,args in (
            ('tent_load_config_from_file',None,[C.c_char_p]),('tent_create_engine',C.c_void_p,[]),
            ('tent_destroy_engine',None,[C.c_void_p]),
            ('tent_register_memory',C.c_int,[C.c_void_p,C.c_void_p,C.c_size_t]),
            ('tent_unregister_memory',C.c_int,[C.c_void_p,C.c_void_p,C.c_size_t])):
            f=getattr(self.lib,name);f.restype=result;f.argtypes=args
        self.lib.tent_load_config_from_file(os.environ['MC_TENT_CONF'].encode())
        self.handle=self.lib.tent_create_engine()
        if not self.handle:raise RuntimeError('new TENT engine creation failed')

    def allocate_managed_buffer(self,size):
        buffer=C.create_string_buffer(size);address=C.addressof(buffer)
        if self.lib.tent_register_memory(self.handle,address,size):raise RuntimeError('new TENT register failed')
        self.buffers[address]=(buffer,size);return address

    def free_managed_buffer(self,address,size):
        if self.lib.tent_unregister_memory(self.handle,address,size):raise RuntimeError('new TENT unregister failed')
        self.buffers.pop(address)

    def close(self):
        if self.handle:
            self.lib.tent_destroy_engine(self.handle);self.handle=None


def main():
    p=argparse.ArgumentParser(add_help=False,allow_abbrev=False)
    p.add_argument('--tent-library',type=Path,required=True);p.add_argument('--introspection-library',type=Path,required=True)
    p.add_argument('--source-commit',required=True);controls,rest=p.parse_known_args()
    instances=[]
    def engine(args,out):
        ip,netdev=protocol.addresses(args.nic,args.bind_ip)
        mode=Path('/sys/module/erdma/parameters/compat_mode').read_text().strip()
        if mode!='Y':raise RuntimeError('eRDMA compatibility mode differs')
        config=dict(local_segment_name=f'{ip}:{args.rpc_port}',metadata_type='p2p',rpc_server_hostname=ip,
            rpc_server_port=args.rpc_port,log_level='warning',topology=dict(rdma_whitelist=[args.nic]),
            enable_auto_failover_on_poll=False,
            transports={n:dict(enable=n=='rdma') for n in ('rdma','tcp','hp_tcp','shm','nvlink','mnnvl','gds','io_uring','ub','mpcomm','tpu','fabric')},
            policy=[dict(name='pinned_rdma',segment_type='memory',devices=[args.nic],transports=['rdma'])])
        config['transports']['rdma']['num_lanes']=6
        protocol.dump(out/'tent-config.json',config)
        os.environ.update(MC_USE_TENT='1',MC_TENT_CONF=str(out/'tent-config.json'),MC_TE_FILTERS=args.nic,
            MOONCAKE_LOCAL_HOSTNAME=ip,MC_RDMA_BIND_ADDRESS=ip)
        for key in ('MC_TE_FILTERS_EXCLUDE','MC_CUSTOM_TOPO_JSON'):os.environ.pop(key,None)
        e=Engine(controls.tent_library);instances.append(e)
        intro=C.CDLL(str(controls.introspection_library));intro.tent_test_topology.restype=C.c_int
        intro.tent_test_topology.argtypes=[C.c_void_p,C.c_char_p,C.c_size_t]
        buffer=C.create_string_buffer(65536)
        if intro.tent_test_topology(e.handle,buffer,len(buffer)):raise RuntimeError('topology introspection failed')
        topology=json.loads(buffer.value);names=[n['name'] for n in topology['nics']]
        if names!=[args.nic]:raise RuntimeError('actual receiver NICs differ: '+str(names))
        info=dict(backend='TENT C API from merged main',source_commit=controls.source_commit,
            python=sys.executable,conda_env=os.environ.get('CONDA_DEFAULT_ENV'),nic=args.nic,netdev=netdev,ip=ip,
            session=f'{ip}:{args.rpc_port}',rdma_nics=names,rdma_num_lanes=6,erdma_compat_mode=mode,
            module=str(controls.tent_library),module_sha256=hashlib.sha256(controls.tent_library.read_bytes()).hexdigest(),
            receiver_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),transport_policy=['rdma'])
        protocol.dump(out/'environment.json',info);protocol.dump(out/'topology.json',topology)
        return e,info
    protocol.engine=engine;sys.argv=[sys.argv[0],*rest]
    try:protocol.main()
    finally:
        for e in instances:e.close()


if __name__=='__main__':main()
