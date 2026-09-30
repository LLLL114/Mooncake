#!/usr/bin/env python3
"""Reuse the verified single-rail/dual-rail calibration in a fresh TG directory."""
import calibrate_rate_batch as calibration

calibration.ROOT = calibration.BASE / 'tail-gain-20260930'

if __name__ == '__main__':
    calibration.main()
