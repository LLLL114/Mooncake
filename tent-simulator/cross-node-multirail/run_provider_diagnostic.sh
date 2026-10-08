#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root=/root/mooncake-tent-multirdma-output/cross-node-multirail/provider-diagnostic-20261008
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1 TENT_NUMA_NODE=0 TENT_OBS_QUEUE_POLL_STRIDE=256
[[ ! -e "$root/pipeline.stage" ]] || { echo 'Existing pipeline; inspect first.' >&2; exit 2; }
trap 'rc=$?; echo "finished $rc" > "$root/pipeline.exit"' EXIT
for stage in pilot overhead formal; do
  echo "$stage" > "$root/pipeline.stage"
  numactl --cpunodebind=0 --membind=0 python -u -B "$here/run_provider_diagnostic.py" --stage "$stage" > "$root/$stage.log" 2>&1
  echo "$stage-analysis" > "$root/pipeline.stage"
  python -u -B "$here/analyze_provider_diagnostic.py" --stage "$stage" > "$root/$stage-analysis.log" 2>&1
done
echo complete > "$root/pipeline.stage"
