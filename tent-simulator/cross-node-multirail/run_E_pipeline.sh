#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1
root=/root/mooncake-tent-multirdma-output/cross-node-multirail/E-20260917
mkdir -p "$root/logs"
trap 'rc=$?; printf "finished %s\n" "$rc" > "$root/pipeline.exit"' EXIT
for stage in overhead steady dynamic; do
  printf '%s measurement\n' "$stage" > "$root/pipeline.stage"
  bash "$here/run_E.sh" --stage "$stage" > "$root/logs/$stage-measurement.log" 2>&1
  printf '%s analysis\n' "$stage" > "$root/pipeline.stage"
  python -u -B "$here/analyze_E.py" --stage "$stage" > "$root/logs/$stage-analysis.log" 2>&1
done
printf 'complete\n' > "$root/pipeline.stage"
