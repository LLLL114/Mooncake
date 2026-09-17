#!/usr/bin/env python3
"""Configure candidates before engine creation; export after native drain."""
import argparse
import ctypes
import os
from pathlib import Path
import sys
import native_sender_extended as sender


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--candidate-policy', type=int, choices=range(4), required=True)
    control, rest = parser.parse_known_args()
    original_bindings, original_run = sender.bindings, sender.run
    loaded = []

    def bindings(lib):
        original_bindings(lib)
        lib.tent_cand_configure.argtypes = [ctypes.c_int]
        lib.tent_cand_configure.restype = ctypes.c_int
        lib.tent_osc_configure.argtypes = [ctypes.c_int, ctypes.c_double, ctypes.c_uint32]
        lib.tent_osc_configure.restype = ctypes.c_int
        lib.tent_osc_dump.argtypes = [ctypes.c_char_p]
        lib.tent_osc_dump.restype = ctypes.c_int
        lib.tent_osc_error.restype = ctypes.c_int
        if lib.tent_cand_configure(control.candidate_policy) or lib.tent_osc_configure(0, 0., 200000):
            raise RuntimeError('candidate setup failed')
        loaded.append((lib, Path(os.environ['MC_TENT_CONF']).parent))

    def export():
        if not loaded:
            raise RuntimeError('library was not loaded')
        lib, out = loaded[0]
        rc, error = lib.tent_osc_dump(str(out).encode()), lib.tent_osc_error()
        if rc or error:
            raise RuntimeError(f'candidate trace export invalid: {rc}, {error}')

    def run(args):
        args.candidate_policy = control.candidate_policy
        if args.config_override or args.callers != 1 or args.size != 1048576 or args.alpha != .01:
            raise ValueError('candidate protocol requires original priors, one caller, 1MiB, alpha .01')
        try:
            original_run(args)
        except BaseException:
            try:
                export()
            except Exception as error:
                print('CAND_EXPORT_ERROR', error, flush=True)
            raise
        export()
        print('CAND_TRACE_EXPORTED', flush=True)

    sender.bindings, sender.run = bindings, run
    sys.argv = [sys.argv[0], *rest]
    sender.main()


if __name__ == '__main__':
    main()
