#!/usr/bin/env python3
"""Configure the E-only learning gate before the frozen sender creates TENT."""
import argparse
import ctypes
import sys

import native_sender_extended as sender


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--learning-stride', type=int, required=True)
    options, rest = parser.parse_known_args()
    if options.learning_stride not in (1, 10, 100):
        parser.error('learning stride must be 1, 10 or 100')
    original_bindings, original_run = sender.bindings, sender.run
    loaded = []

    def bindings(lib):
        original_bindings(lib)
        lib.tent_e_set_stride.argtypes = [ctypes.c_uint]
        lib.tent_e_set_stride.restype = ctypes.c_int
        lib.tent_e_error.argtypes = []
        lib.tent_e_error.restype = ctypes.c_int
        if lib.tent_e_set_stride(options.learning_stride):
            raise RuntimeError('learning stride rejected')
        loaded.append(lib)

    def run(args):
        args.learning_stride = options.learning_stride
        original_run(args)
        if len(loaded) != 1 or loaded[0].tent_e_error():
            raise RuntimeError('learning controller error; reject this run')

    sender.bindings, sender.run = bindings, run
    sys.argv = [sys.argv[0], *rest]
    sender.main()


if __name__ == '__main__':
    main()
