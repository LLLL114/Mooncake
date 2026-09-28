"""Opt-in real Store tests. MC_KV_STORE_NATIVE=1 enables owned local services."""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import socket
import subprocess
import time
import traceback
import uuid
from pathlib import Path

import pytest
from mooncake.reshard.kv_cache import (
    KVCacheStore,
    KVCacheStoreFormat,
    KVCacheTransferLimits,
    plan_kv_cache_store_upload,
)
from test_kv_cache_reshard import _placement
from test_store_contracts import _manifest
from test_store_execution import memory_binding

pytestmark = pytest.mark.skipif(
    os.environ.get("MC_KV_STORE_NATIVE") != "1",
    reason="set MC_KV_STORE_NATIVE=1 for actual Store tests",
)


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(process, port, logfile):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(Path(logfile).read_text()[-6000:])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"service on port {port} did not start")


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _real(addresses, *, segment_size=0):
    from mooncake.store import MooncakeDistributedStore

    store = MooncakeDistributedStore()
    result = store.setup(
        f"127.0.0.1:{_port()}",
        addresses[1],
        segment_size,
        32 << 20,
        "tcp",
        "",
        addresses[0],
    )
    if result != 0:
        raise RuntimeError(f"Store setup failed: {result}")
    return store


@pytest.fixture
def cluster(tmp_path):
    build = Path(
        os.environ.get(
            "MC_KV_STORE_BUILD",
            str(Path(__file__).resolve().parents[3] / "build-kv-reshard-r2s"),
        )
    )
    master = build / "mooncake-store/src/mooncake_master"
    assert master.is_file(), f"missing native master: {master}"
    ports = set()
    while len(ports) < 3:
        ports.add(_port())
    rpc, http, metrics = sorted(ports)
    logfile = tmp_path / "master.log"
    with logfile.open("w") as log:
        process = subprocess.Popen(
            [
                str(master),
                f"--port={rpc}",
                "--rpc_address=127.0.0.1",
                "--rpc_thread_num=4",
                "--enable_metric_reporting=false",
                f"--metrics_port={metrics}",
                "--enable_http_metadata_server=true",
                "--http_metadata_server_host=127.0.0.1",
                f"--http_metadata_server_port={http}",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    provider = None
    try:
        _wait(process, rpc, logfile)
        _wait(process, http, logfile)
        addresses = (f"127.0.0.1:{rpc}", f"http://127.0.0.1:{http}/metadata")
        provider = _real(addresses, segment_size=64 << 20)
        yield addresses, build, tmp_path
    finally:
        if provider is not None:
            provider.close()
        _stop(process)


@contextlib.contextmanager
def _bound(
    store,
    placement,
    participant,
    fmt,
    pages,
    operation,
    *,
    fill=True,
    shared=False,
    salts=None,
):
    binding, buffers, expected = memory_binding(
        placement,
        participant,
        fmt,
        pages,
        operation,
        fill=fill,
        allocator=store.alloc_from_mem_pool if shared else None,
        page_salts=salts,
    )
    registered = []
    try:
        for region in binding.regions:
            assert store.register_buffer(region.address, region.nbytes) == 0
            registered.append(region)
        yield binding, buffers, expected
    finally:
        for region in registered:
            assert store.unregister_buffer(region.address) == 0


def test_native_metadata_conditional_update(cluster):
    addresses, _, _ = cluster
    store = _real(addresses)
    key = "native-cas-" + uuid.uuid4().hex
    try:
        assert store.read_metadata_for_update(key) == (0, b"", "")
        assert store.compare_exchange_metadata(key, "", b"first\x00\xff") == 1
        code, value, token = store.read_metadata_for_update(key)
        assert code == 1 and value == b"first\x00\xff" and token
        assert store.compare_exchange_metadata(key, token, b"second-longer-value") == 1
        assert store.compare_exchange_metadata(key, token, b"stale") == 0
        code, value, new_token = store.read_metadata_for_update(key)
        assert code == 1 and value == b"second-longer-value" and new_token != token
    finally:
        store.close()


@pytest.mark.parametrize("fmt", tuple(KVCacheStoreFormat))
def test_native_single_process_tp_pp_restore(cluster, fmt):
    addresses, _, _ = cluster
    raw = _real(addresses)
    placement = _placement("source", ((0,), (1, 2), (3,)), 1)
    target = _placement("target", ((0, 1), (2, 3)), 2)
    plan = plan_kv_cache_store_upload(placement, object_format=fmt)
    writer = KVCacheStore(raw, _manifest(plan.layout))
    keys = tuple(uuid.uuid4().hex for _ in range(2))
    try:
        writer.register_layout()
        for part in placement.parts:
            with _bound(raw, placement, part.participant_id, fmt, 2, "upload") as (
                binding,
                _,
                _,
            ):
                assert writer.upload(plan, keys, binding, operation_id="upload") == (
                    True,
                    True,
                )
        reader = KVCacheStore(
            raw, _manifest(plan_kv_cache_store_upload(target, object_format=fmt).layout)
        )
        context = reader.discover(keys, operation_id="read")
        assert len(context.page_keys) == 2
        for part in target.parts:
            with _bound(
                raw, target, part.participant_id, fmt, 2, "read", fill=False
            ) as (binding, buffers, expected):
                assert reader.load(context, target, binding) == 2
                assert [bytes(buffer) for buffer in buffers] == expected
    finally:
        raw.close()


@pytest.mark.parametrize("prepared", [False, True])
def test_native_multi_batch_snapshot_restore(cluster, prepared):
    addresses, _, _ = cluster
    raw = _real(addresses)
    fmt = KVCacheStoreFormat.PLHD
    source = _placement("snapshot-source", ((0, 1),), 1)
    target = _placement("snapshot-target", ((0, 1),), 2)
    plan = plan_kv_cache_store_upload(source, object_format=fmt)
    writer = KVCacheStore(raw, _manifest(plan.layout))
    key = uuid.uuid4().hex

    class CountedStore:
        def __init__(self):
            self.snapshots = []
            self.reads = []

        def __getattr__(self, name):
            return getattr(raw, name)

        def prepare_get_into_ranges_snapshot(self, keys):
            snapshot = raw.prepare_get_into_ranges_snapshot(keys)
            self.snapshots.append(snapshot)
            return snapshot

        def get_into_ranges_from_snapshot(self, snapshot, *args):
            self.reads.append(snapshot)
            return raw.get_into_ranges_from_snapshot(snapshot, *args)

        def get_into_ranges(self, *args):
            raise AssertionError("unexpected ordinary ranged read")

    counted = CountedStore()
    try:
        writer.register_layout()
        with _bound(raw, source, source.parts[0].participant_id, fmt, 1, "put") as (
            binding,
            _,
            _,
        ):
            assert writer.upload(plan, (key,), binding, operation_id="put") == (True,)
        reader = KVCacheStore(
            counted,
            _manifest(plan_kv_cache_store_upload(target, object_format=fmt).layout),
            transfer_limits=KVCacheTransferLimits(max_batch_operations=2),
        )
        context = reader.discover((key,), operation_id="get")
        with _bound(
            raw, target, target.parts[0].participant_id, fmt, 1, "get", fill=False
        ) as (binding, buffers, expected):
            page_reader = (
                reader.prepare_page_reader(target, binding) if prepared else None
            )
            for _ in range(2):
                previous_reads = len(counted.reads)
                hits = (
                    page_reader.load(
                        context, [{r.region_id: 0 for r in binding.regions}]
                    )
                    if page_reader is not None
                    else reader.load(context, target, binding)
                )
                assert hits == 1
                assert [bytes(buffer) for buffer in buffers] == expected
                assert len(counted.reads) - previous_reads > 1
                assert all(
                    s is counted.snapshots[-1] for s in counted.reads[previous_reads:]
                )
            assert len(counted.snapshots) == 2
    finally:
        raw.close()


def _worker(request, queue):
    raw = None
    try:
        raw = _real(request["addresses"])
        placement = _placement(request["name"], request["layers"], request["tp"])
        part = placement.parts[request["rank"]]
        fmt = KVCacheStoreFormat.PLHD
        plan = plan_kv_cache_store_upload(placement, object_format=fmt)
        store = KVCacheStore(raw, _manifest(plan.layout))
        if request["mode"] == "upload":
            store.register_layout()
            with _bound(
                raw,
                placement,
                part.participant_id,
                fmt,
                1,
                "upload",
                salts=(request["salt"],),
            ) as (binding, _, _):
                assert store.upload(
                    plan, (request["key"],), binding, operation_id="upload"
                ) == (True,)
        else:
            context = request["context"]
            with _bound(
                raw,
                placement,
                part.participant_id,
                fmt,
                2,
                context.operation_id,
                fill=False,
            ) as (binding, buffers, expected):
                assert store.load(context, placement, binding) == 2
                assert [bytes(buffer) for buffer in buffers] == expected
        raw.close()
        raw = None
        queue.put((True, request["name"], request["rank"]))
    except Exception:  # noqa: BLE001 - relay any worker failure to the parent test
        queue.put((False, traceback.format_exc()))
    finally:
        if raw is not None:
            raw.close()


def _run_workers(requests):
    mp = multiprocessing.get_context("spawn")
    queue = mp.Queue()
    workers = [
        mp.Process(target=_worker, args=(request, queue)) for request in requests
    ]
    try:
        for worker in workers:
            worker.start()
        results = [queue.get(timeout=60) for _ in workers]
        assert all(r[0] for r in results), results
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        queue.close()


def test_native_multi_process_sources_survive_uploader_exit(cluster):
    addresses, _, _ = cluster
    keys = tuple(uuid.uuid4().hex for _ in range(2))
    layouts = (
        ("a", (tuple(range(4)), tuple(range(4, 8)), tuple(range(8, 12))), 1),
        ("b", (tuple(range(6)), tuple(range(6, 12))), 2),
    )
    requests = []
    for salt, (name, layers, tp) in enumerate(layouts):
        for rank in range(len(layers) * tp):
            requests.append(
                {
                    "mode": "upload",
                    "addresses": addresses,
                    "name": name,
                    "layers": layers,
                    "tp": tp,
                    "rank": rank,
                    "key": keys[salt],
                    "salt": salt,
                }
            )
    _run_workers(requests)
    # The uploader processes are gone; only the independent Store provider remains.
    raw = _real(addresses)
    try:
        target_layers = (tuple(range(6)), tuple(range(6, 12)))
        target = _placement("target", target_layers, 2)
        reader = KVCacheStore(
            raw,
            _manifest(
                plan_kv_cache_store_upload(
                    target, object_format=KVCacheStoreFormat.PLHD
                ).layout
            ),
        )
        context = reader.discover(keys, operation_id="shared-read")
        assert (
            len(context.page_keys) == 2
            and context.layout_ids[0] != context.layout_ids[1]
        )
        _run_workers(
            [
                {
                    "mode": "load",
                    "addresses": addresses,
                    "name": "target",
                    "layers": target_layers,
                    "tp": 2,
                    "rank": rank,
                    "context": context,
                }
                for rank in range(4)
            ]
        )
    finally:
        raw.close()


def test_native_dummy_client_uses_its_own_local_allocator(cluster):
    from mooncake.store import MooncakeDistributedStore

    addresses, build, tmp_path = cluster
    rpc, te = _port(), _port()
    logfile = tmp_path / "proxy.log"
    with logfile.open("w") as log:
        proxy = subprocess.Popen(
            [
                str(build / "mooncake-store/src/mooncake_client"),
                f"--port={rpc}",
                f"--host=127.0.0.1:{te}",
                "--protocol=tcp",
                "--threads=4",
                "--global_segment_size=0",
                "--local_buffer_size=0",
                f"--master_server_address={addresses[0]}",
                f"--metadata_server={addresses[1]}",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    raw = None
    try:
        _wait(proxy, rpc, logfile)
        raw = MooncakeDistributedStore()
        assert raw.setup_dummy(16 << 20, 16 << 20, f"127.0.0.1:{rpc}") == 0
        placement = _placement("dummy", ((0, 1),), 1)
        plan = plan_kv_cache_store_upload(
            placement, object_format=KVCacheStoreFormat.PLHD
        )
        store = KVCacheStore(raw, _manifest(plan.layout))
        store.register_layout()
        key = uuid.uuid4().hex
        with _bound(
            raw,
            placement,
            placement.parts[0].participant_id,
            KVCacheStoreFormat.PLHD,
            1,
            "put",
            shared=True,
        ) as (binding, _, _):
            assert store.upload(plan, (key,), binding, operation_id="put") == (True,)
        context = store.discover((key,), operation_id="get")
        with _bound(
            raw,
            placement,
            placement.parts[0].participant_id,
            KVCacheStoreFormat.PLHD,
            1,
            "get",
            shared=True,
            fill=False,
        ) as (binding, buffers, expected):
            assert store.load(context, placement, binding) == 1
            assert [bytes(buffer) for buffer in buffers] == expected
    finally:
        if raw is not None:
            raw.close()
        _stop(proxy)
