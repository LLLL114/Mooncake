#!/usr/bin/env python3
"""SSH receiver only: preserve the frozen default receiver; create lane=1 copy."""
import ast
import hashlib
import json
from pathlib import Path

EXPECTED='c8c8735d60a1bb145c01a979b813e865d30682dba48e629d3b6c96f32722ee92'


def main():
    here=Path(__file__).resolve().parent
    source=here/'receiver_extended.py';raw=source.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=EXPECTED:raise ValueError('frozen receiver differs')
    text=raw.decode()
    changes=[
        ("    dump(out / 'tent-config.json', config)",
         "    config['transports']['rdma']['num_lanes'] = 1\n    dump(out / 'tent-config.json', config)"),
        ("        'backend': 'TENT via MC_USE_TENT=1', 'native_topology_keys': sorted(topology),",
         "        'backend': 'TENT via MC_USE_TENT=1', 'native_topology_keys': sorted(topology),\n        'rdma_num_lanes': 1, 'receiver_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"),
        ("p.add_argument('role', choices=['receiver', 'sender'])", "p.add_argument('role', choices=['receiver'])")]
    for old,new in changes:
        if text.count(old)!=1:raise ValueError('receiver source anchor differs')
        text=text.replace(old,new)
    ast.parse(text)
    target=here/'receiver_lanes1.py'
    with target.open('x') as stream:stream.write(text)
    receipt=Path('/root/mooncake-tent-multirdma-output/cross-node-multirail/protocols/receiver-lanes1.json')
    receipt.parent.mkdir(parents=True,exist_ok=True)
    with receipt.open('x') as stream:json.dump(dict(source_sha256=EXPECTED,output_sha256=hashlib.sha256(text.encode()).hexdigest(),
        changes='num_lanes=1, explicit receiver metadata, receiver-only CLI; original package unchanged'),stream,indent=2)
    print('RECEIVER_LANES1_PREPARED',hashlib.sha256(text.encode()).hexdigest())


if __name__=='__main__':main()
