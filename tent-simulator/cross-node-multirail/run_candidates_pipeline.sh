#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root=/root/mooncake-tent-multirdma-output/cross-node-multirail/candidates-20260917
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1
if [[ -e "$root/pipeline.stage" || -e "$root/formal.log" ]]; then
  echo 'Existing pipeline outputs; inspect before resuming.' >&2
  exit 2
fi
trap 'rc=$?; echo "finished $rc" > "$root/pipeline.exit"' EXIT
echo formal > "$root/pipeline.stage"
bash "$here/run_candidates.sh" --stage formal > "$root/formal.log" 2>&1
echo analysis > "$root/pipeline.stage"
python -u -B "$here/analyze_candidates.py" > "$root/analysis.log" 2>&1
echo figures > "$root/pipeline.stage"
python -u -B "$here/plot_candidates.py" > "$root/figures.log" 2>&1
echo complete > "$root/pipeline.stage"
