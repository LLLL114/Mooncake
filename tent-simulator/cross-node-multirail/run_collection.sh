#!/usr/bin/env bash
# Run on A10_8 only. Resumes this baseline without retrying collected failures.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1 TENT_NUMA_NODE=0 TENT_OBS_QUEUE_POLL_STRIDE=256
root=/root/mooncake-tent-multirdma-output/cross-node-multirail/suites-extended
exec numactl --cpunodebind=0 --membind=0 python -u -B "$here/run_suite_collection.py" \
  --phase baseline --collect-unexpected-overload --output-root "$root" \
  --capacity "$root/capacity.json"
