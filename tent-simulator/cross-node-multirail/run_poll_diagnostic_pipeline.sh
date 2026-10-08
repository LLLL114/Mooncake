#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root=/root/mooncake-tent-multirdma-output/cross-node-multirail/poll-diagnostic-20260930
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1
[[ ! -e "$root/pipeline.stage" ]] || { echo 'Existing pipeline; inspect first.' >&2; exit 2; }
trap 'rc=$?; echo "finished $rc" > "$root/pipeline.exit"' EXIT
for stage in pilot overhead; do
  echo "$stage" > "$root/pipeline.stage"
  bash "$here/run_poll_diagnostic.sh" --stage "$stage" > "$root/$stage.log" 2>&1
done
echo overhead-analysis > "$root/pipeline.stage"
python -u -B "$here/analyze_poll_diagnostic.py" --stage overhead > "$root/overhead-analysis.log" 2>&1
echo formal > "$root/pipeline.stage"
bash "$here/run_poll_diagnostic.sh" --stage formal > "$root/formal.log" 2>&1
echo analysis > "$root/pipeline.stage"
python -u -B "$here/analyze_poll_diagnostic.py" --stage formal > "$root/analysis.log" 2>&1
echo complete > "$root/pipeline.stage"
