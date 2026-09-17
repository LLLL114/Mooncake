"""Server-only: one actual multi-request C API submission; gate counts only."""
import ctypes as C
import json
import time
from pathlib import Path


class Request(C.Structure):
    _fields_ = [('opcode', C.c_int), ('source', C.c_void_p),
                ('target_id', C.c_uint64), ('target_offset', C.c_uint64),
                ('length', C.c_uint64), ('priority', C.c_int),
                ('transport_hint', C.c_int)]


class Status(C.Structure):
    _fields_ = [('status', C.c_int), ('transferred_bytes', C.c_uint64)]


def run_gate(lib, engine, target, source, destination, stride, config, output):
    count, size = config['window'], config['size']
    signatures = {
        'tent_allocate_batch': (C.c_uint64, [C.c_void_p, C.c_size_t]),
        'tent_submit': (C.c_int, [C.c_void_p, C.c_uint64, C.POINTER(Request), C.c_size_t]),
        'tent_task_status': (C.c_int, [C.c_void_p, C.c_uint64, C.c_size_t, C.POINTER(Status)]),
        'tent_cancel_task': (C.c_int, [C.c_void_p, C.c_uint64, C.c_size_t]),
        'tent_free_batch': (C.c_int, [C.c_void_p, C.c_uint64]),
    }
    for name, (result, arguments) in signatures.items():
        method = getattr(lib, name)
        method.restype, method.argtypes = result, arguments
    requests = (Request * count)()
    for i in range(count):
        addr = source + i * stride + 64
        C.memmove(addr, (1).to_bytes(8, 'little'), 8)
        C.memmove(addr + size - 8, (1).to_bytes(8, 'little'), 8)
        requests[i] = Request(1, addr, target, destination + i * stride + 64, size, 0, 1)
    start = time.monotonic_ns() + 1_000_000_000
    if hasattr(lib, 'tent_obs_configure'):
        lib.tent_obs_configure.argtypes = [C.c_int, C.c_uint64, C.c_uint64]
        lib.tent_obs_configure.restype = None
        lib.tent_obs_configure(1, start, start + 40_000_000_000)
    remaining = (start - time.monotonic_ns()) / 1e9
    if remaining <= 0:
        raise RuntimeError('gate observation preparation missed fixed start')
    time.sleep(remaining)
    batch = lib.tent_allocate_batch(engine, count)
    if not batch:
        raise RuntimeError('gate batch allocation failed')
    submitted = time.monotonic_ns()
    submit_rc = lib.tent_submit(engine, batch, requests, count)
    records, epochs = {}, [None] * count
    canceled = False
    deadline = submitted + 30_000_000_000
    while len(records) != count and time.monotonic_ns() < deadline + 5_000_000_000:
        if time.monotonic_ns() >= deadline and not canceled:
            for i in range(count):
                if i not in records:
                    lib.tent_cancel_task(engine, batch, i)
            canceled = True
        for i in range(count):
            if i in records:
                continue
            status = Status()
            rc = lib.tent_task_status(engine, batch, i, C.byref(status))
            if rc == 0 and status.status in (3, 4, 5, 6):
                ok = status.status == 4 and submit_rc == 0 and not canceled
                epochs[i] = 1 if ok else None
                records[i] = {'request_id': i, 'flow_id': 0, 'planned_ns': submitted,
                              'submitted_ns': submitted, 'finished_ns': time.monotonic_ns(),
                              'bytes': size, 'status': 'success' if ok else 'failure',
                              'native_status': status.status, 'source_slot': i, 'epoch': 1}
    end = time.monotonic_ns() + 1
    pending = count - len(records)
    cleanup_failed = not pending and lib.tent_free_batch(engine, batch) != 0
    success = sum(r['status'] == 'success' for r in records.values())
    rc = -4 if pending or cleanup_failed else 0 if success == count else -2
    summary = {'measurement_start_ns': submitted, 'measurement_end_ns': end,
               'accepted': count, 'success': success, 'failure': len(records)-success,
               'pending': pending + int(cleanup_failed), 'final_epochs': epochs,
               'return_code': rc, 'scope': 'gate_counts_only_not_a_bandwidth_sample',
               'api_submit_calls': 1, 'api_batch_requests': count,
               'observer_window_scope': 'fixed upper bound; use counts, not event rates'}
    output = Path(output)
    (output / 'stream-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    (output / 'requests.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records.values()))
    return rc
