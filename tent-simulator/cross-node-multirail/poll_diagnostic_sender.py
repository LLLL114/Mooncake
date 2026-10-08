#!/usr/bin/env python3
"""Enable timing before engine creation; drain/join before binary export."""
import argparse
import ctypes
import os
from pathlib import Path
import sys
import native_sender_extended as sender


def main():
    parser=argparse.ArgumentParser(add_help=False,allow_abbrev=False)
    parser.add_argument('--poll-diagnostic',action='store_true',required=True)
    controls,rest=parser.parse_known_args()
    original_bindings,original_run=sender.bindings,sender.run
    loaded=[]
    def bindings(lib):
        original_bindings(lib)
        lib.tent_pd_configure.argtypes=[];lib.tent_pd_configure.restype=ctypes.c_int
        lib.tent_osc_configure.argtypes=[ctypes.c_int,ctypes.c_double,ctypes.c_uint32]
        lib.tent_osc_configure.restype=ctypes.c_int
        for name in ('tent_pd_dump','tent_osc_dump'):
            getattr(lib,name).argtypes=[ctypes.c_char_p];getattr(lib,name).restype=ctypes.c_int
        if lib.tent_pd_configure() or lib.tent_osc_configure(0,0.,200000): raise RuntimeError('PD configure failed')
        loaded.append((lib,Path(os.environ['MC_TENT_CONF']).parent))
    def export():
        if not loaded: raise RuntimeError('no PD library')
        lib,out=loaded[0]
        a,b=lib.tent_pd_dump(str(out).encode()),lib.tent_osc_dump(str(out).encode())
        if a or b: raise RuntimeError(f'PD export invalid: {a}, {b}')
    def run(args):
        if args.callers!=1 or args.size!=1048576 or args.config_override or args.alpha!=.01:
            raise ValueError('PD requires unchanged original algorithm, CPU 1MiB single caller')
        args.poll_diagnostic=controls.poll_diagnostic
        # On failure workers may still be live; serialize their non-atomic
        # buffers only after normal teardown has returned.
        original_run(args)
        export();print('PD_EXPORTED',flush=True)
    sender.bindings,sender.run=bindings,run
    sys.argv=[sys.argv[0],*rest];sender.main()


if __name__=='__main__': main()
