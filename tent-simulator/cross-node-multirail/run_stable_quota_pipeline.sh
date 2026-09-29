#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root=/root/mooncake-tent-multirdma-output/cross-node-multirail/stable-quota-20260929
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1
if [[ -e "$root/pipeline.stage" ]]; then
  echo 'Existing pipeline: inspect before restarting.' >&2
  exit 2
fi
trap 'rc=$?; echo "finished $rc" > "$root/pipeline.exit"' EXIT
for stage in pilot formal saturated; do
  echo "$stage" > "$root/pipeline.stage"
  bash "$here/run_stable_quota.sh" --stage "$stage" > "$root/$stage.log" 2>&1
done
echo analysis > "$root/pipeline.stage"
python -u -B "$here/analyze_stable_quota.py" > "$root/analysis.log" 2>&1
echo figures > "$root/pipeline.stage"
python -u -B "$here/plot_stable_quota.py" > "$root/figures.log" 2>&1
echo complete > "$root/pipeline.stage"
