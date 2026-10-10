"""Independent rank writers, one key, all-parts visibility and exact range reads.

Run against an owned memory-only Master:
  python multipart_demo.py --master 127.0.0.1:50051 --metadata http://127.0.0.1:8080/metadata
"""

import argparse
import ctypes
import multiprocessing as mp
import socket
import uuid

# Required import order in the kvmanifest SGLang environment.
import torch  # noqa: F401
from mooncake.store import MooncakeDistributedStore


def connect(master, metadata, segment=0):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    store = MooncakeDistributedStore()
    assert (
        store.setup(f"127.0.0.1:{port}", metadata, segment, 4 << 20, "tcp", "", master)
        == 0
    )
    return store


def writer(master, metadata, key, ref, index, count):
    store = connect(master, metadata)
    data = ctypes.create_string_buffer(bytes([index + 1]) * 4096, 4096)
    address = ctypes.addressof(data)
    try:
        assert store.register_buffer(address, len(data)) == 0
        assert store.batch_put_parts_from(
            [key], index, count, ref, [[address]], [[len(data)]]
        ) == [0]
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", required=True)
    parser.add_argument("--metadata", required=True)
    args = parser.parse_args()
    store = connect(args.master, args.metadata, 64 << 20)
    key, count = "multipart-demo-" + uuid.uuid4().hex, 3
    ref = key + "/manifest"
    try:
        assert store.put(ref, b"demo: three independent 4096-byte parts") == 0
        for index in range(count):
            process = mp.get_context("spawn").Process(
                target=writer, args=(args.master, args.metadata, key, ref, index, count)
            )
            process.start()
            process.join(60)
            if process.is_alive():
                process.terminate()
                process.join()
                raise TimeoutError("part writer did not complete")
            assert process.exitcode == 0
            assert store.batch_is_exist([key]) == [int(index == count - 1)]
        assert store.batch_query_parts([key]) == [(0, ref, count)]
        out = ctypes.create_string_buffer(4096 * count)
        address = ctypes.addressof(out)
        assert store.register_buffer(address, len(out)) == 0
        snapshot = store.prepare_get_parts_snapshot([key], [ref], [count])
        templates = [
            store.prepare_get_into_ranges_template([i * 4096], [0], [4096])
            for i in range(count)
        ]
        result = store.get_into_ranges_from_template(
            snapshot,
            templates,
            [address],
            [0] * count,
            [key + f"\x1fp{i}" for i in range(count)],
            [0] * count,
        )
        assert result == [True] * count
        assert bytes(out) == b"".join(bytes([i + 1]) * 4096 for i in range(count))
        # A different topology cannot overwrite a complete logical object.
        assert store.batch_put_parts_from(
            [key], 0, 4, "different-layout", [[address]], [[4096]]
        ) == [0]
        assert store.batch_query_parts([key]) == [(0, ref, count)]
        print("MULTIPART_DEMO_PASSED")
    finally:
        store.close()


if __name__ == "__main__":
    main()
