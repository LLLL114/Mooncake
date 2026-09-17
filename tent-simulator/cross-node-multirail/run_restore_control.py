#!/usr/bin/env python3
"""SSH-only same-day default-lane restoration control, separate from A samples."""
import argparse
import fcntl
import json
from pathlib import Path
import socket
import sys

import run_suite_collection_v2 as suite

BASE=Path('/root/mooncake-tent-multirdma-output/cross-node-multirail')


def main():
    args=argparse.Namespace(peer='10.0.1.251',latency_window_ms=1000,seconds=10,warmup=1,
        tent_library=str(BASE/'build-v2/libtent_shared.so'),stream_library=str(BASE/'build-v2/stream_native.so'),
        output_root=BASE/'restore-default-20260916')
    with socket.create_connection((args.peer,19930),timeout=10) as sock:
        with sock.makefile('rwb') as stream:
            stream.write(b'{"op":"hello"}\n');stream.flush();reply=json.loads(stream.readline(1048576))
    peer=reply.get('environment',{})
    if (reply.get('ok') is not True or peer.get('rdma_num_lanes')==1 or peer.get('nic')!='erdma_0'
            or peer.get('module_sha256')!='1029698b8287b5fe2d7b28d9e3fd63c3febae5990800813153488cd71c62979d'):
        raise ValueError('default receiver not restored')
    cap=suite.load_capacity(BASE/'suites-extended/capacity.json',args);context=suite._context(args)
    args.output_root.mkdir(parents=True,exist_ok=True)
    with (args.output_root/'.suite.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        progress=dict(complete=False,planned=3,outcomes=[])
        for repeat in (1,2,3):
            case=suite._case(args,context,'restore','D0',1048576,cap['sizes']['1048576']['q'],repeat,'saturated',rate=0)
            case.update(scope='restored default-lane same-day control; not an additional A baseline repeat')
            result=suite.run_case(case,args.output_root)
            progress['outcomes'].append(result);suite._save(args.output_root/'progress.json',progress)
            if result['status']!='success':return 1
            root=Path(result['runPath']);m=suite._read(root/'manifest.json')
            if m.get('receiver',{}).get('rdma_num_lanes')==1 or suite._read(root/'observer.json').get('incomplete') is not False:
                raise ValueError('restoration evidence invalid')
        progress['complete']=True;suite._save(args.output_root/'progress.json',progress)
        print('DEFAULT_RESTORATION_COMPLETE',3,flush=True)
    return 0


if __name__=='__main__':sys.exit(main())
