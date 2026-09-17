#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${CONDA_DEFAULT_ENV:-}" != mooncake ]]; then
  if command -v conda >/dev/null 2>&1; then
    conda_root="$(conda info --base)"
  else
    conda_root=/root/miniconda3
  fi
  source "$conda_root/etc/profile.d/conda.sh"
  conda activate mooncake
fi
export PYTHONDONTWRITEBYTECODE=1
# A bounded process lifetime also covers a stuck native call or teardown.
exec timeout --signal=TERM --kill-after=10s "${TENT_TEST_TIMEOUT:-1900}" \
  python -u -B "$here/tent_link_check.py" "$@"
