#!/usr/bin/env python3
"""Configure O controls before engine creation; export trace only after teardown."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys

import native_sender_extended as sender


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--fixed-equal', action='store_true')
    parser.add_argument('--symmetric-prior', action='store_true')
    controls, rest = parser.parse_known_args()
    original_bindings, original_run = sender.bindings, sender.run
    loaded = []

    def bindings(lib):
        original_bindings(lib)
        lib.tent_osc_configure.argtypes = [ctypes.c_int, ctypes.c_double, ctypes.c_uint32]
        lib.tent_osc_configure.restype = ctypes.c_int
        lib.tent_osc_dump.argtypes = [ctypes.c_char_p]
        lib.tent_osc_dump.restype = ctypes.c_int
        lib.tent_osc_error.argtypes = []
        lib.tent_osc_error.restype = ctypes.c_int
        rc = lib.tent_osc_configure(int(controls.fixed_equal), 10. if controls.symmetric_prior else 0., 200000)
        if rc:
            raise RuntimeError('oscillation control configure failed: ' + str(rc))
        loaded.append((lib, Path(os.environ['MC_TENT_CONF']).parent))

    def export(required):
        if not loaded:
            return
        lib, out = loaded[0]
        rc = lib.tent_osc_dump(str(out).encode())
        error = lib.tent_osc_error()
        if rc or error:
            if required:
                raise RuntimeError(f'trace export invalid: {rc}, {error}')
            print('OSC_TRACE_FAILURE', rc, error, flush=True)

    def run(args):
        args.fixed_equal = controls.fixed_equal
        args.symmetric_prior = controls.symmetric_prior
        expected = [1, 1, 1] if controls.symmetric_prior else None
        actual = json.loads(args.config_override or '{}').get('numa_penalties')
        if actual != expected or args.callers != 1 or args.size != 1048576:
            raise ValueError('O protocol requires matching NUMA override, one caller and 1MiB')
        try:
            original_run(args)
        except BaseException:
            export(False)
            raise
        export(True)
        print('OSC_TRACE_EXPORTED', str(loaded[0][1]), flush=True)

    sender.bindings, sender.run = bindings, run
    sys.argv = [sys.argv[0], *rest]
    sender.main()


if __name__ == '__main__':
    main()
