#!/usr/bin/env bash
# A10 only. Preflight rejects the default lane=6 receiver before data transfer.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1 TENT_NUMA_NODE=0 TENT_OBS_QUEUE_POLL_STRIDE=256
exec numactl --cpunodebind=0 --membind=0 python -u -B "$here/run_C_resources.py" "$@"
