"""Server-only source preparation for Q32/64/128; preserve original drivers."""
import argparse
from pathlib import Path


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('source anchor mismatch: ' + old)
    return text.replace(old, new)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=['sender', 'receiver'])
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    if args.role == 'receiver':
        text = (here / 'pilot.py').read_text()
        text = replace_once(text, '1 <= args.slots <= 32', '1 <= args.slots <= 128')
        target = here / 'receiver_extended.py'
        with target.open('x') as stream:
            stream.write(text)
    else:
        text = (here / 'native_sender.py').read_text()
        text = replace_once(text, '1<=a.callers<=a.window<=32', '1<=a.callers<=a.window<=128')
        text = replace_once(text, "'warmup_drain_seconds':5", "'warmup_drain_seconds':10")
        with (here / 'native_sender_extended.py').open('x') as stream:
            stream.write(text)
        text = (here / 'run_suite.py').read_text()
        text = replace_once(text, 'SENDER = HERE / "native_sender.py"',
                            'SENDER = HERE / "native_sender_extended.py"')
        text = replace_once(text, 'QS = (1, 8, 32)', 'QS = (32, 64, 128)')
        text = replace_once(text, 'd0["32"] / d0["8"] - 1', 'd0["128"] / d0["64"] - 1')
        text = replace_once(text, 'd0["32"] <= d0["8"] * 1.05', 'd0["128"] <= d0["64"] * 1.05')
        text = text.replace('q32_vs_q8_growth_fraction', 'q128_vs_q64_growth_fraction')
        text = text.replace('Q32 median exceeds Q8', 'Q128 median exceeds Q64')
        with (here / 'run_suite_extended.py').open('x') as stream:
            stream.write(text)
    print('EXTENDED_SOURCE_PREPARED', args.role, flush=True)


if __name__ == '__main__':
    main()
