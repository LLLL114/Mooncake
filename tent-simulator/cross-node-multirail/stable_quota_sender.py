#!/usr/bin/env python3
"""Single-caller SQ controls; no transport or workload modifications."""
import argparse
import ctypes
import os
from pathlib import Path
import sys
import native_sender_extended as sender


def main():
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--stable-policy', type=int, choices=(1, 2, 3, 4), required=True)
    parser.add_argument('--rail0-bps', type=float, required=True)
    parser.add_argument('--rail1-bps', type=float, required=True)
    controls, rest = parser.parse_known_args()
    original_bindings, original_run = sender.bindings, sender.run
    loaded = []

    def bindings(lib):
        original_bindings(lib)
        lib.tent_sq_configure.argtypes = [ctypes.c_int, ctypes.c_double, ctypes.c_double]
        lib.tent_sq_configure.restype = ctypes.c_int
        lib.tent_osc_configure.argtypes = [ctypes.c_int, ctypes.c_double, ctypes.c_uint32]
        lib.tent_osc_configure.restype = ctypes.c_int
        for name in ('tent_osc_dump', 'tent_sq_dump'):
            getattr(lib, name).argtypes = [ctypes.c_char_p]
            getattr(lib, name).restype = ctypes.c_int
        if lib.tent_sq_configure(controls.stable_policy, controls.rail0_bps, controls.rail1_bps):
            raise RuntimeError('SQ configure failed')
        if lib.tent_osc_configure(0, 0., 200000):
            raise RuntimeError('trace configure failed')
        loaded.append((lib, Path(os.environ['MC_TENT_CONF']).parent))

    def export():
        if not loaded:
            raise RuntimeError('no loaded SQ library')
        lib, out = loaded[0]
        a, b = lib.tent_sq_dump(str(out).encode()), lib.tent_osc_dump(str(out).encode())
        if a or b:
            raise RuntimeError(f'SQ export failed: {a}, {b}')

    def run(args):
        args.stable_policy = controls.stable_policy
        args.rail0_bps, args.rail1_bps = controls.rail0_bps, controls.rail1_bps
        if args.config_override or args.callers != 1 or args.size != 1048576 or args.alpha != .01:
            raise ValueError('SQ requires original config, one caller, 1MiB and alpha .01')
        try:
            original_run(args)
        except BaseException:
            try:
                export()
            except Exception as error:
                print('SQ_EXPORT_ERROR', error, flush=True)
            raise
        export()
        print('SQ_EXPORTED', flush=True)

    sender.bindings, sender.run = bindings, run
    sys.argv = [sys.argv[0], *rest]
    sender.main()


if __name__ == '__main__':
    main()
