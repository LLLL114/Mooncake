#!/usr/bin/env bash
# Execute only on the A10 sender; all data remains outside the Git repository.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1 TENT_NUMA_NODE=0 TENT_OBS_QUEUE_POLL_STRIDE=256
exec numactl --cpunodebind=0 --membind=0 python -u -B "$here/run_followups_bc.py" "$@"
