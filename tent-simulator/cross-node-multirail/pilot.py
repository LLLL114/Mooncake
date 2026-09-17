#!/usr/bin/env python3
"""Cross-node TENT path pilot through the installed package.
This pilot verifies dual-NIC data paths; internal scheduling metrics require the observed runtime.
"""
import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys
import time
import uuid

GUARD = 64
POISON = 0xA5
MAX_MESSAGE = 65536


def dump(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def addresses(nic, explicit):
    import subprocess
    rows = json.loads(subprocess.check_output(['rdma', '-j', 'link', 'show']))
    row = next((r for r in rows if r.get('ifname') == nic), None)
    if row is None:
        # rdma versions may expose the device under a different field.
        row = next((r for r in rows if r.get('name', '').split('/')[0] == nic), None)
    if row is None:
        raise RuntimeError(f'RDMA device {nic} not found: {rows}')
    netdev = row['netdev']
    ips = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show', 'dev', netdev]))
    choices = [a['local'] for r in ips for a in r.get('addr_info', []) if a['scope'] == 'global']
    ip = explicit or choices[0]
    if ip not in choices:
        raise ValueError(f'{ip} does not belong to {nic}/{netdev}: {choices}')
    return ip, netdev


def counters():
    wanted = {'hw_tx_bytes_cnt', 'hw_rx_bytes_cnt', 'hw_tx_reqs_cnt',
              'hw_tx_packets_cnt', 'hw_rx_packets_cnt', 'connect_failed_cnt',
              'connect_timeout_cnt', 'hw_bps_limit_drop_cnt', 'hw_rx_bps_limit_drop_cnt'}
    answer = {}
    for folder in Path('/sys/class/infiniband').glob('erdma_*/ports/1/hw_counters'):
        values = {}
        for p in folder.iterdir():
            if p.name in wanted:
                try:
                    values[p.name] = int(p.read_text().strip())
                except (ValueError, OSError):
                    pass
        answer[folder.parts[-4]] = values
    return answer


def delta(before, after):
    return {nic: {k: after[nic][k] - v for k, v in values.items() if k in after.get(nic, {})}
            for nic, values in before.items() if nic in after}


def tag(seed, slot, epoch):
    value = hashlib.sha256(f'{seed}:{slot}:{epoch}'.encode()).digest()
    return bytes([epoch % 251]) + value[1:]


def payload(seed, slot, size, epoch):
    unit = hashlib.shake_256(f'{seed}:{slot}:body'.encode()).digest(4096)
    data = bytearray((unit * ((size + 4095) // 4096))[:size])
    marker = tag(seed, slot, epoch)
    n = min(32, size)
    data[:n] = marker[:n]
    tail = min(32, size - n)
    if tail:
        data[-tail:] = marker[-tail:]
    return bytes(data)


def stamp(base, stride, lengths, seed, epoch):
    for slot, size in enumerate(lengths):
        address = base + slot * stride + GUARD
        marker = tag(seed, slot, epoch)
        n = min(32, size)
        ctypes.memmove(address, marker, n)
        tail = min(32, size - n)
        if tail:
            ctypes.memmove(address + size - tail, marker[-tail:], tail)


def engine(args, out):
    ip, netdev = addresses(args.nic.split(',')[0], args.bind_ip)
    mode_path = Path('/sys/module/erdma/parameters/compat_mode')
    compat_mode = mode_path.read_text().strip() if mode_path.exists() else None
    if compat_mode == 'N':
        raise RuntimeError('eRDMA compat_mode=N: this TENT direct-QP test requires the compatible driver mode; have the administrator enable compat_mode=1 and reload the driver before retrying')
    config = {
        'local_segment_name': f'{ip}:{args.rpc_port}',
        'metadata_type': 'p2p', 'rpc_server_hostname': ip,
        'rpc_server_port': args.rpc_port, 'log_level': 'warning',
        'topology': {'rdma_whitelist': args.nic.split(',')},
        'enable_auto_failover_on_poll': False,
        'transports': {n: {'enable': n == 'rdma'} for n in
                       ['rdma', 'tcp', 'hp_tcp', 'shm', 'nvlink', 'mnnvl',
                        'gds', 'io_uring', 'ub', 'mpcomm', 'tpu']},
        'policy': [{'name': 'pinned_rdma', 'segment_type': 'memory',
                    'devices': args.nic.split(','), 'transports': ['rdma']}],
    }
    dump(out / 'tent-config.json', config)
    os.environ['MC_USE_TENT'] = '1'
    os.environ['MC_TENT_CONF'] = str(out / 'tent-config.json')
    os.environ['MC_TE_FILTERS'] = args.nic
    os.environ.pop('MC_TE_FILTERS_EXCLUDE', None)
    os.environ.pop('MC_CUSTOM_TOPO_JSON', None)
    os.environ['MOONCAKE_LOCAL_HOSTNAME'] = ip
    os.environ['MC_RDMA_BIND_ADDRESS'] = ip
    os.environ['MC_TRANSFER_TIMEOUT'] = '30'
    import mooncake.engine as module
    instance = module.TransferEngine()
    topology = json.loads(instance.get_local_topology(args.nic))
    if not {'nics', 'mems'}.issubset(topology):
        raise RuntimeError(f'TENT native topology was not returned: {list(topology)}')
    names = [r['name'] for r in topology['nics'] if r['name'].startswith('erdma_')]
    if sorted(names) != sorted(args.nic.split(',')):
        raise RuntimeError(f'NIC restriction not effective: wanted {args.nic}, got {names}')
    rc = instance.initialize(f'{ip}:{args.rpc_port}', 'P2PHANDSHAKE', 'rdma', args.nic)
    if rc != 0:
        raise RuntimeError(f'TENT initialize returned {rc}')
    session = f'{ip}:{instance.get_rpc_port()}'
    binary = Path(module.__file__)
    info = {
        'python': sys.executable, 'conda_env': os.environ.get('CONDA_DEFAULT_ENV'),
        'package_version': importlib.metadata.version('mooncake-transfer-engine'),
        'module': str(binary), 'module_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'backend': 'TENT via MC_USE_TENT=1', 'native_topology_keys': sorted(topology),
        'erdma_compat_mode': compat_mode,
        'nic': args.nic, 'netdev': netdev, 'ip': ip, 'session': session,
        'rdma_nics': names, 'transport_policy': ['rdma'],
    }
    dump(out / 'environment.json', info)
    dump(out / 'topology.json', topology)
    return instance, info


def send(file, value):
    file.write(json.dumps(value, separators=(',', ':')).encode() + b'\n')
    file.flush()


def receive(file):
    line = file.readline(MAX_MESSAGE + 1)
    if not line:
        raise EOFError('control connection closed')
    if len(line) > MAX_MESSAGE or not line.endswith(b'\n'):
        raise ValueError('invalid control message')
    return json.loads(line)


def rpc(file, value):
    send(file, value)
    result = receive(file)
    if not result.get('ok'):
        raise RuntimeError(result.get('error', 'receiver rejected request'))
    return result


def receiver(args, out):
    te, info = engine(args, out)
    stride = args.max_bytes + 2 * GUARD
    capacity = stride * args.slots
    base = te.allocate_managed_buffer(capacity)
    if not base:
        raise RuntimeError('receiver memory registration failed')
    reports = []
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((info['ip'], args.control_port))
    server.listen(4)
    server.settimeout(1)
    stop = False
    deadline = time.monotonic() + args.server_seconds
    print(f'RECEIVER_READY {info["ip"]}:{args.control_port} TENT={info["session"]} NIC={args.nic}', flush=True)
    try:
        while not stop and time.monotonic() < deadline:
            try:
                conn, peer = server.accept()
            except socket.timeout:
                continue
            active = None
            conn.settimeout(120)
            with conn, conn.makefile('rwb') as file:
                while not stop:
                    try:
                        request = receive(file)
                    except EOFError:
                        break
                    try:
                        operation = request['op']
                        response = {'ok': True}
                        if operation == 'hello':
                            response.update(environment=info, base=base, slots=args.slots,
                                            max_bytes=args.max_bytes, stride=stride)
                        elif operation == 'prepare':
                            lengths = request['lengths']
                            assert 0 < len(lengths) <= args.slots
                            assert all(isinstance(n, int) and 0 < n <= args.max_bytes for n in lengths)
                            assert isinstance(request['seed'], str) and len(request['seed']) <= 128
                            ctypes.memset(base, POISON, capacity)
                            active = {'seed': request['seed'], 'lengths': lengths}
                            response['poisoned'] = True
                        elif operation == 'snapshot':
                            response['counters'] = counters()
                        elif operation == 'verify':
                            assert active and request['seed'] == active['seed']
                            epoch = request['epoch']
                            checks = []
                            for slot, size in enumerate(active['lengths']):
                                address = base + slot * stride + GUARD
                                expected = hashlib.sha256(payload(active['seed'], slot, size, epoch)).hexdigest()
                                actual = hashlib.sha256(ctypes.string_at(address, size)).hexdigest()
                                guard_ok = (ctypes.string_at(address-GUARD, GUARD) == bytes([POISON])*GUARD
                                            and ctypes.string_at(address+size, GUARD) == bytes([POISON])*GUARD)
                                checks.append({'slot': slot, 'bytes': size, 'sha256': actual,
                                               'expected_sha256': expected, 'guard_ok': guard_ok,
                                               'passed': actual == expected and guard_ok})
                            response.update(passed=all(c['passed'] for c in checks), checks=checks,
                                            counters=counters())
                            reports.append({'seed': active['seed'], 'epoch': epoch,
                                            'peer': peer[0], **response})
                            dump(out / 'receiver-checks.json', reports)
                            print('VERIFIED', active['seed'], epoch, response['passed'], flush=True)
                        elif operation == 'stop':
                            stop = True
                        else:
                            raise ValueError('unknown control operation')
                        send(file, response)
                    except Exception as error:
                        send(file, {'ok': False, 'error': f'{type(error).__name__}: {error}'})
                        raise
    finally:
        server.close()
        te.free_managed_buffer(base, capacity)
        dump(out / 'receiver-summary.json', {'environment': info, 'checks': len(reports),
                                           'all_passed': all(r['passed'] for r in reports), 'stopped': stop})
    print('RECEIVER_DONE', len(reports), flush=True)


def sender(args, out):
    te, info = engine(args, out)
    connection = socket.create_connection((args.peer, args.control_port), timeout=15)
    connection.settimeout(120)
    rows = []
    with connection, connection.makefile('rwb') as file:
        remote = rpc(file, {'op': 'hello'})
        if remote['environment']['backend'] != info['backend']:
            raise RuntimeError('backend mismatch')
        dump(out / 'receiver-environment.json', remote['environment'])
        stride = remote['stride']
        capacity = stride * args.window
        if args.window > remote['slots'] or max(args.sizes) > remote['max_bytes']:
            raise ValueError('receiver buffer limits are smaller than requested')
        base = te.allocate_managed_buffer(capacity)
        if not base:
            raise RuntimeError('sender memory registration failed')
        def prepare(lengths, label):
            seed = f'{args.nic}-{label}-{uuid.uuid4().hex}'
            rpc(file, {'op': 'prepare', 'seed': seed, 'lengths': lengths})
            for slot, size in enumerate(lengths):
                data = payload(seed, slot, size, 0)
                ctypes.memmove(base + slot*stride + GUARD, data, size)
            local = [base+i*stride+GUARD for i in range(len(lengths))]
            target = [remote['base']+i*stride+GUARD for i in range(len(lengths))]
            return seed, local, target
        def transfer(local, target, lengths):
            begin = time.perf_counter_ns()
            rc = te.batch_transfer_sync_write(remote['environment']['session'], local, target, lengths, 'rdma')
            elapsed = time.perf_counter_ns() - begin
            if rc != 0:
                raise RuntimeError(f'TENT batch transfer failed: {rc}')
            return elapsed
        def verify(seed, epoch):
            result = rpc(file, {'op': 'verify', 'seed': seed, 'epoch': epoch})
            if not result['passed']:
                raise RuntimeError(f'receiver payload/guard verification failed: {result}')
            return result
        try:
            mixed = sorted({min(n, remote['max_bytes']) for n in [1, 4096, 65536, 983040, 999424, 1048576, 16777216]})[:args.window]
            seed, local, target = prepare(mixed, 'correctness')
            strict = []
            for epoch in range(3):
                stamp(base, stride, mixed, seed, epoch)
                elapsed = transfer(local, target, mixed)
                strict.append({'epoch': epoch, 'transfer_ns': elapsed, **verify(seed, epoch)})
            dump(out / 'correctness.json', {'lengths': mixed, 'rounds': strict, 'passed': True})
            print('CONNECTIVITY_PASS', args.nic, '->', remote['environment']['nic'], flush=True)
            if not args.check_only:
                for size in args.sizes:
                    for repeat in range(args.repeats):
                        lengths = [size] * args.window
                        seed, local, target = prepare(lengths, f'{size}-r{repeat}')
                        epoch = 0
                        warm_end = time.monotonic() + args.warmup
                        while time.monotonic() < warm_end:
                            stamp(base, stride, lengths, seed, epoch)
                            transfer(local, target, lengths)
                            epoch += 1
                        verify(seed, epoch-1)
                        receiver_before = rpc(file, {'op': 'snapshot'})['counters']
                        before = counters()
                        calls = []
                        start = time.perf_counter_ns()
                        while time.perf_counter_ns() - start < args.seconds*1e9:
                            stamp(base, stride, lengths, seed, epoch)
                            calls.append(transfer(local, target, lengths))
                            epoch += 1
                        elapsed = time.perf_counter_ns() - start
                        after = counters()
                        verified = verify(seed, epoch-1)
                        nbytes = len(calls)*sum(lengths)
                        row = {'source_nic': args.nic, 'target_nic': remote['environment']['nic'],
                               'size': size, 'window': args.window, 'repeat': repeat,
                               'completed_batches': len(calls), 'completed_requests': len(calls)*args.window,
                               'payload_bytes': nbytes, 'elapsed_ns': elapsed, 'api_ns': sum(calls),
                               'goodput_gbps': nbytes*8/elapsed, 'api_goodput_gbps': nbytes*8/sum(calls),
                               'gib_per_second': nbytes/(2**30)/(elapsed/1e9),
                               'batch_latency_ns': calls, 'final_epoch': epoch-1, 'verified': True,
                               'sender_counters': delta(before, after),
                               'receiver_counters': delta(receiver_before, verified['counters'])}
                        rows.append(row)
                        dump(out / 'bandwidth.json', rows)
                        print(f'BANDWIDTH nic={args.nic} bytes={size} repeat={repeat} '
                              f'Gbps={row["goodput_gbps"]:.3f} API_Gbps={row["api_goodput_gbps"]:.3f} '
                              f'GiBps={row["gib_per_second"]:.3f} verified=true', flush=True)
            if args.stop_receiver:
                rpc(file, {'op': 'stop'})
        finally:
            te.free_managed_buffer(base, capacity)
    dump(out / 'summary.json', {'environment': info, 'receiver': remote['environment'],
                                'correctness_passed': True, 'bandwidth_cases': len(rows),
                                'rows': [{k:v for k,v in r.items() if k!='batch_latency_ns'} for r in rows],
                                'scope': 'installed TENT pilot; CPU DRAM; NIC set from arguments; internal scheduling metrics not collected',
                                'timing': 'stream goodput includes marker updates and Python/API overhead; excludes setup, warmup, TCP control and hashing; API goodput excludes between-call marker updates',
                                'verification': 'three mixed-size rounds checked before reuse; bandwidth batches all complete; warmup and final full payload SHA256 plus guards checked, not every overwritten bandwidth payload'} )
    print('SENDER_DONE', out, flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('role', choices=['receiver', 'sender'])
    p.add_argument('--nic', default='erdma_0', help='erdma_0, erdma_1, or erdma_0,erdma_1')
    p.add_argument('--bind-ip')
    p.add_argument('--peer', default='10.0.1.251')
    p.add_argument('--rpc-port', type=int)
    p.add_argument('--control-port', type=int, default=19930)
    p.add_argument('--window', type=int, default=32)
    p.add_argument('--slots', type=int, default=32)
    p.add_argument('--max-bytes', type=int, default=16777216)
    p.add_argument('--sizes', type=int, nargs='+', default=[65536, 1048576, 16777216])
    p.add_argument('--seconds', type=float, default=5)
    p.add_argument('--warmup', type=float, default=.5)
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--server-seconds', type=float, default=1800)
    p.add_argument('--check-only', action='store_true')
    p.add_argument('--stop-receiver', action='store_true')
    p.add_argument('--output-root', default='/root/mooncake-tent-multirdma-output/cross-node-multirail/pilot-runs')
    args = p.parse_args()
    if args.nic not in ['erdma_0', 'erdma_1', 'erdma_0,erdma_1']:
        p.error('supported NIC sets: erdma_0 / erdma_1 / erdma_0,erdma_1')
    if not (1 <= args.window <= 32 and 1 <= args.slots <= 32 and
            1 <= args.max_bytes <= 16777216 and all(1 <= n <= 16777216 for n in args.sizes)
            and 0 < args.warmup <= 10 and 0 < args.seconds <= 60 and 1 <= args.repeats <= 10):
        p.error('invalid bounded test parameters')
    args.rpc_port = args.rpc_port or (19931 if args.role == 'receiver' else 19932)
    output_root = Path(args.output_root).expanduser().resolve()
    repo = Path(__file__).resolve().parents[2]
    if output_root == repo or repo in output_root.parents:
        p.error('output-root must be outside the Mooncake repository')
    out = output_root / f'{args.role}-{args.nic}-{time.strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:6]}'
    out.mkdir(parents=True, exist_ok=False)
    dump(out / 'arguments.json', vars(args))
    print('OUTPUT', out, flush=True)
    try:
        (receiver if args.role == 'receiver' else sender)(args, out)
    except Exception as error:
        dump(out / 'failure.json', {'type': type(error).__name__, 'error': str(error)})
        raise


if __name__ == '__main__':
    main()
