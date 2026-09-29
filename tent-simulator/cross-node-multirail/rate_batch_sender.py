#!/usr/bin/env python3
"""Configure frozen RB controls before engine creation and export after drain."""
import argparse
import ctypes
import os
from pathlib import Path
import sys
import native_sender_extended as sender


def parse_controls(argv=None):
    p = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    p.add_argument('--rate-batch-policy', type=int, choices=range(6), required=True)
    p.add_argument('--rail0-bps', type=float, required=True)
    p.add_argument('--rail1-bps', type=float, required=True)
    return p.parse_known_args(argv)


def main():
    controls, rest = parse_controls()
    original_bindings, original_run = sender.bindings, sender.run
    loaded = []

    def bindings(lib):
        original_bindings(lib)
        lib.tent_rb_configure.argtypes = [ctypes.c_int, ctypes.c_double, ctypes.c_double]
        lib.tent_rb_configure.restype = ctypes.c_int
        lib.tent_osc_configure.argtypes = [ctypes.c_int, ctypes.c_double, ctypes.c_uint32]
        lib.tent_osc_configure.restype = ctypes.c_int
        for name in ('tent_osc_dump', 'tent_rb_dump'):
            getattr(lib, name).argtypes = [ctypes.c_char_p]
            getattr(lib, name).restype = ctypes.c_int
        lib.tent_osc_error.restype = ctypes.c_int
        if lib.tent_rb_configure(controls.rate_batch_policy, controls.rail0_bps, controls.rail1_bps):
            raise RuntimeError('RB configure failed')
        if lib.tent_osc_configure(0, 0., 200000):
            raise RuntimeError('trace configure failed')
        loaded.append((lib, Path(os.environ['MC_TENT_CONF']).parent))

    def export():
        if not loaded:
            raise RuntimeError('library not loaded')
        lib, out = loaded[0]
        a, b = lib.tent_rb_dump(str(out).encode()), lib.tent_osc_dump(str(out).encode())
        if a or b or lib.tent_osc_error():
            raise RuntimeError(f'RB export/audit error: {a}, {b}, {lib.tent_osc_error()}')

    def run(args):
        args.rate_batch_policy = controls.rate_batch_policy
        args.rail0_bps, args.rail1_bps = controls.rail0_bps, controls.rail1_bps
        if args.config_override or args.callers != 1 or args.size != 1048576 or args.alpha != .01:
            raise ValueError('RB requires original config, one caller, 1MiB and alpha .01')
        try:
            original_run(args)
        except BaseException:
            try:
                export()
            except Exception as error:
                print('RB_EXPORT_ERROR', error, flush=True)
            raise
        export()
        print('RB_EXPORTED', flush=True)

    sender.bindings, sender.run = bindings, run
    sys.argv = [sys.argv[0], *rest]
    sender.main()


if __name__ == '__main__':
    main()
