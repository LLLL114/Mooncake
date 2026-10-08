#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root=/root/mooncake-tent-multirdma-output/cross-node-multirail/reference-rail-controls-20261008
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1 TENT_NUMA_NODE=0 TENT_OBS_QUEUE_POLL_STRIDE=256
[[ ! -e "$root/pipeline.stage" ]] || { echo 'Existing pipeline; inspect first.' >&2; exit 2; }
trap 'rc=$?; echo "finished $rc" > "$root/pipeline.exit"' EXIT
echo collection > "$root/pipeline.stage"
numactl --cpunodebind=0 --membind=0 python -u -B "$here/run_reference_rail_controls.py" > "$root/collection.log" 2>&1
echo analysis > "$root/pipeline.stage"
python -u -B "$here/analyze_reference_rail_controls.py" > "$root/analysis.log" 2>&1
echo complete > "$root/pipeline.stage"
