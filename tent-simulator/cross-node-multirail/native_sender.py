#!/usr/bin/env python3
"""Run the installed-version full TENT runtime through its C API."""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import socket
import time
import uuid
from pilot import addresses,counters,delta,dump,payload,rpc,GUARD


def memory_placement(address):
    for line in Path('/proc/self/maps').read_text().splitlines():
        bounds=line.split()[0].split('-')
        if int(bounds[0],16)<=address<int(bounds[1],16):
            numa_line=next((r for r in Path('/proc/self/numa_maps').read_text().splitlines()
                            if r.startswith(bounds[0]+' ')),None)
            return {'cpu_affinity':sorted(os.sched_getaffinity(0)),
                    'source_buffer_vma':line,'source_buffer_numa_map':numa_line,
                    'scope':'VMA containing source buffer; may include adjacent allocations'}
    return {'cpu_affinity':sorted(os.sched_getaffinity(0)),'source_buffer_vma':None}


def bindings(lib):
    P=C.c_void_p;U=C.c_uint64;I=C.c_int;S=C.c_size_t;T=C.c_char_p
    signatures={'tent_load_config_from_file':(None,[T]),'tent_create_engine':(P,[]),
        'tent_destroy_engine':(None,[P]),'tent_open_segment':(I,[P,C.POINTER(U),T]),
        'tent_close_segment':(I,[P,U]),'tent_register_memory':(I,[P,P,S]),
        'tent_unregister_memory':(I,[P,P,S]),'tent_available':(I,[P])}
    for name,(restype,args) in signatures.items():
        f=getattr(lib,name);f.restype=restype;f.argtypes=args
    if hasattr(lib,'tent_obs_dump'):
        lib.tent_obs_dump.argtypes=[T];lib.tent_obs_dump.restype=None
        if hasattr(lib,'tent_obs_last_error'):
            lib.tent_obs_last_error.argtypes=[];lib.tent_obs_last_error.restype=I


def run(args):
    if os.environ.get('TENT_OBS_QUEUE_POLL_STRIDE','1') not in ('1','256'):
        raise ValueError('TENT_OBS_QUEUE_POLL_STRIDE must be 1 or 256')
    repo=Path(__file__).resolve().parents[2]
    root=Path(args.output_root).resolve()
    if root==repo or repo in root.parents:raise ValueError('output must be outside repository')
    out=root/(args.label+'-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
    out.mkdir(parents=True);print('OUTPUT',out,flush=True)
    dump(out/'arguments.json',vars(args))
    ip,netdev=addresses(args.nic.split(',')[0],None)
    mode=Path('/sys/module/erdma/parameters/compat_mode').read_text().strip()
    if mode!='Y':raise RuntimeError('eRDMA compatible driver mode is required')
    config={'local_segment_name':f'{ip}:19932','metadata_type':'p2p','rpc_server_hostname':ip,
       'rpc_server_port':19932,'log_level':'warning','topology':{'rdma_whitelist':args.nic.split(',')},
       'enable_auto_failover_on_poll':False,
       'transports':{n:{'enable':n=='rdma'} for n in ['rdma','tcp','hp_tcp','shm','nvlink','mnnvl','gds','io_uring','ub','mpcomm','tpu']},
       'policy':[{'name':'test_rdma','segment_type':'memory','devices':args.nic.split(','),'transports':['rdma']}]}
    rdma=config['transports']['rdma'];rdma['enable_smart_scheduling']=not args.rr
    if args.alpha is not None:rdma['bandwidth_learning_rate']=args.alpha
    if args.workers is not None:rdma['num_lanes']=args.workers
    if args.jitter is not None:rdma['score_jitter_range']=args.jitter
    if args.default_gbps is not None:rdma['default_bandwidth_gbps']=args.default_gbps
    if args.config_override:rdma.update(json.loads(args.config_override))
    dump(out/'tent-config.json',config)
    os.environ.update(MC_USE_TENT='1',MC_TENT_CONF=str(out/'tent-config.json'),MC_TE_FILTERS=args.nic,
                      MOONCAKE_LOCAL_HOSTNAME=ip,MC_RDMA_BIND_ADDRESS=ip)
    for name in ['MC_TE_FILTERS_EXCLUDE','MC_CUSTOM_TOPO_JSON']:os.environ.pop(name,None)
    lib=C.CDLL(args.tent_library,mode=C.RTLD_GLOBAL);bindings(lib)
    native=C.CDLL(args.stream_library)
    native.tent_test_stream.argtypes=[C.c_void_p,C.c_uint64,C.c_uint64,C.c_uint64,C.c_uint64,C.c_char_p,C.c_char_p]
    native.tent_test_stream.restype=C.c_int
    lib.tent_load_config_from_file(str(out/'tent-config.json').encode())
    engine=lib.tent_create_engine()
    if not engine:raise RuntimeError('TENT create failed')
    target=C.c_uint64();storage=None;registered=False;unsafe=False
    try:
        introspection=C.CDLL(str(Path(args.stream_library).parent/'native_introspection.so'))
        introspection.tent_test_topology.argtypes=[C.c_void_p,C.c_char_p,C.c_size_t]
        introspection.tent_test_topology.restype=C.c_int
        topology_buffer=C.create_string_buffer(65536)
        if introspection.tent_test_topology(engine,topology_buffer,len(topology_buffer)):
            raise RuntimeError('cannot read actual TENT engine topology')
        topology=json.loads(topology_buffer.value.decode())
        dump(out/'topology.json',topology)
        nic_names={str(i):n['name'] for i,n in enumerate(topology['nics'])}
        if sorted(nic_names.values())!=sorted(args.nic.split(',')):
            raise RuntimeError('actual engine NIC set differs from requested whitelist')
        with socket.create_connection((args.peer,19930),timeout=15) as conn:
            conn.settimeout(180)
            with conn.makefile('rwb') as f:
                remote=rpc(f,{'op':'hello'})
                assert args.window<=remote['slots'] and args.size<=remote['max_bytes']
                stride=remote['stride'];size=stride*args.window
                storage=C.create_string_buffer(size);base=C.addressof(storage)
                if lib.tent_register_memory(engine,base,size):raise RuntimeError('memory registration failed')
                registered=True
                if lib.tent_open_segment(engine,C.byref(target),remote['environment']['session'].encode()):raise RuntimeError('open segment failed')
                seed=f'native-{uuid.uuid4().hex}'
                lengths=[args.size]*args.window
                rpc(f,{'op':'prepare','seed':seed,'lengths':lengths})
                for slot in range(args.window):
                    data=payload(seed,slot,args.size,0)
                    C.memmove(base+slot*stride+GUARD,data,args.size)
                placement=memory_placement(base)
                dump(out/'memory-placement.json',placement)
                stream={'size':args.size,'window':args.window,'callers':args.callers,'seconds':args.seconds,
                        'warmup':args.warmup,'rate_bytes_per_second':args.rate,'deadline_seconds':30,
                        'observer_enabled':not args.observer_off,'queue_capacity':16384,'max_lateness_seconds':30,
                        'observer_prepare_seconds':5,'warmup_drain_seconds':5,
                        'arrival_alignment':args.arrival_alignment}
                if args.steps:stream['steps']=json.loads(args.steps)
                dump(out/'stream-config.json',stream)
                before=counters();remote_before=rpc(f,{'op':'snapshot'})['counters']
                unsafe=True
                if args.gate_batch:
                    from gate_probe import run_gate
                    rc=run_gate(lib,engine,target.value,base,remote['base'],stride,stream,out)
                else:
                    rc=native.tent_test_stream(engine,target.value,base,remote['base'],stride,
                                               json.dumps(stream).encode(),str(out).encode())
                after=counters()
                summary=json.loads((out/'stream-summary.json').read_text())
                unsafe=bool(summary.get('pending',0))
                if unsafe:raise RuntimeError('native stream did not drain; process must exit without reusing/freeing buffers')
                dump(out/'manifest.json',{**summary,'native_return_code':rc,'data_verified':None,
                    'label':args.label,'stream_config':stream,'sender_counters':delta(before,after),
                    'receiver_verification':'pending; checkpoint preserves failed-run accounting'})
                if hasattr(lib,'tent_obs_dump'):
                    lib.tent_obs_dump(str(out/'observer.json').encode())
                    if hasattr(lib,'tent_obs_last_error') and lib.tent_obs_last_error():
                        raise RuntimeError('observer export failed')
                verified=rpc(f,{'op':'verify_epochs','seed':seed,'epochs':summary['final_epochs']})
                dump(out/'correctness.json',verified)
                if not verified['passed']:raise RuntimeError('receiver final full payload/guard mismatch')
                manifest={**summary,'environment':{'nic':args.nic,'ip':ip,'netdev':netdev,'compat_mode':mode,
                    'nic_id_to_name':nic_names,
                    'cpu_affinity':placement['cpu_affinity'],'numa_node_requested':os.environ.get('TENT_NUMA_NODE'),
                    'observer_queue_poll_stride_requested':os.environ.get('TENT_OBS_QUEUE_POLL_STRIDE','1'),
                    'python':os.sys.executable,'tent_library':args.tent_library,'tent_sha256':hashlib.sha256(Path(args.tent_library).read_bytes()).hexdigest(),
                    'stream_library':args.stream_library,'stream_sha256':hashlib.sha256(Path(args.stream_library).read_bytes()).hexdigest()},
                    'receiver':remote['environment'],'sender_counters':delta(before,after),
                    'receiver_counters':delta(remote_before,verified['counters']),'data_verified':True,
                    'counter_scope':'entire native stream incl warmup/drain; not measurement-only',
                    'native_return_code':rc,'stream_config':stream,'label':args.label}
                dump(out/'manifest.json',manifest)
                from summarize_requests import summarize_requests
                all_records=[json.loads(line) for line in (out/'requests.jsonl').read_text().splitlines() if line]
                records=[r for r in all_records if r.get('submitted_ns') is not None and r.get('finished_ns') is not None and r.get('status') in ['success','failure']]
                excluded=[r for r in all_records if r.get('submitted_ns') is None or r.get('finished_ns') is None or r.get('status') not in ['success','failure']]
                if excluded:dump(out/'unsubmitted-or-unresolved.json',excluded)
                totals,windows=summarize_requests(records,manifest,latency_window_ms=args.latency_window_ms)
                totals['excluded_non_submitted_terminal_records']=len(excluded)
                totals['complete_run_accounting']=summary
                dump(out/'summary.json',totals);dump(out/'windows.json',windows)
                print('NATIVE_DONE',out,'RC',rc,'VERIFIED',verified['passed'],flush=True)
                if rc:raise RuntimeError(f'native stream returned {rc}; see saved terminal accounting')
    except Exception as e:
        dump(out/'failure.json',{'type':type(e).__name__,'error':str(e),'pending_unsafe':unsafe})
        if unsafe:
            print('UNSAFE_PENDING_EXIT',flush=True);os._exit(2)
        raise
    finally:
        if registered:lib.tent_unregister_memory(engine,C.addressof(storage),len(storage))
        if target.value:lib.tent_close_segment(engine,target.value)
        lib.tent_destroy_engine(engine)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--nic',choices=['erdma_0','erdma_1','erdma_0,erdma_1'],default='erdma_0,erdma_1')
    p.add_argument('--peer',default='10.0.1.251');p.add_argument('--size',type=int,default=1048576)
    p.add_argument('--window',type=int,default=32);p.add_argument('--callers',type=int,default=1)
    p.add_argument('--arrival-alignment',choices=['synchronized','staggered'],default='staggered')
    p.add_argument('--seconds',type=float,default=60);p.add_argument('--warmup',type=float,default=10)
    p.add_argument('--rate',type=float,default=0);p.add_argument('--steps');p.add_argument('--label',default='D0')
    p.add_argument('--rr',action='store_true');p.add_argument('--observer-off',action='store_true')
    p.add_argument('--gate-batch',action='store_true',help='one batch submission; counts-only gate diagnostic')
    p.add_argument('--alpha',type=float);p.add_argument('--workers',type=int);p.add_argument('--jitter',type=float)
    p.add_argument('--default-gbps',type=float);p.add_argument('--config-override');p.add_argument('--latency-window-ms',type=int,default=1000)
    base='/root/mooncake-tent-multirdma-output/cross-node-multirail/'
    p.add_argument('--tent-library',default=base+'build-v2/libtent_shared.so')
    p.add_argument('--stream-library',default=base+'build-v2/stream_native.so')
    p.add_argument('--output-root',default=base+'runs')
    a=p.parse_args()
    if not (16<=a.size<=16777216 and 1<=a.callers<=a.window<=32 and 0<a.seconds<=600 and 0<=a.warmup<=60):p.error('test bounds exceeded')
    run(a)

if __name__=='__main__':main()
