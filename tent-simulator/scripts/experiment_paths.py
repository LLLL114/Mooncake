"""Paths for source files and external experiment artifacts."""
import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SOURCE_ROOT.parent
LEGACY_ROOT = REPO_ROOT / 'experiments' / 'rdma-multirail-baseline'
OUTPUT_ROOT = Path(os.environ.get(
    'MOONCAKE_TENT_OUTPUT', '/root/mooncake-tent-multirdma-output'
)).expanduser().resolve()
if OUTPUT_ROOT == REPO_ROOT or REPO_ROOT in OUTPUT_ROOT.parents:
    raise ValueError('MOONCAKE_TENT_OUTPUT must be outside the Mooncake repository')
ACCEPTANCE_OUTPUT = OUTPUT_ROOT / 'acceptance'
for name in ('build', 'runs', 'reports', 'acceptance', 'cache'):
    (OUTPUT_ROOT / name).mkdir(parents=True, exist_ok=True)


def artifact_key(path):
    path = Path(path).resolve()
    for root, prefix in ((SOURCE_ROOT, ''), (OUTPUT_ROOT, '@output/'), (REPO_ROOT, '@repo/')):
        try:
            return prefix + path.relative_to(root).as_posix()
        except ValueError:
            pass
    raise ValueError('Artifact outside configured roots: ' + str(path))


def artifact_path(key):
    text = str(key)
    if text.startswith('@output/'):
        root, relative = OUTPUT_ROOT, Path(text[8:])
    elif text.startswith('@repo/'):
        root, relative = REPO_ROOT, Path(text[6:])
    else:
        relative = Path(text)
        if relative.is_absolute():
            for old in (LEGACY_ROOT, SOURCE_ROOT):
                try:
                    relative = relative.relative_to(old)
                    break
                except ValueError:
                    pass
            else:
                return relative
        first = relative.parts[0] if relative.parts else ''
        generated = (first == 'acceptance' and relative.suffix in ('.json', '.csv', '.gz', '.md')
                     and relative.name != 'acceptance-plan.md')
        root = OUTPUT_ROOT if first in ('build', 'runs', 'reports') or generated else SOURCE_ROOT
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Invalid artifact key: ' + text)
    return root / relative


def external_output(path):
    path = Path(path).expanduser().resolve()
    if path == REPO_ROOT or REPO_ROOT in path.parents:
        raise ValueError("Output must be outside the Mooncake repository")
    return path
