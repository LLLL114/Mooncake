#!/usr/bin/env python3
"""Build TG extensions with the verified SQ hook placement, in a new output tree."""
import build_stable_quota as build

build.OUT = build.BASE / 'build-tail-gain-20260930'

if __name__ == '__main__':
    build.main()
