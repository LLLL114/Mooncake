#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1
export TENT_NUMA_NODE="${TENT_NUMA_NODE:-0}"
export TENT_OBS_QUEUE_POLL_STRIDE="${TENT_OBS_QUEUE_POLL_STRIDE:-256}"
exec numactl --cpunodebind="$TENT_NUMA_NODE" --membind="$TENT_NUMA_NODE" \
  python -u -B "$here/run_suite.py" "$@"
