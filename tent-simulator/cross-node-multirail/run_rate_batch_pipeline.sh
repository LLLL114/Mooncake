#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root=/root/mooncake-tent-multirdma-output/cross-node-multirail/rate-batch-20260929
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1
resume="${1:-}"
if [[ -n "$resume" && "$resume" != --resume-after-overhead ]]; then
  echo 'Unsupported resume mode.' >&2
  exit 2
fi
if [[ -e "$root/pipeline.stage" || -e "$root/formal.log" || ( -z "$resume" && -e "$root/overhead.log" ) ]]; then
  echo 'Existing pipeline outputs; inspect instead of restarting.' >&2
  exit 2
fi
trap 'rc=$?; echo "finished $rc" > "$root/pipeline.exit"' EXIT
if [[ -z "$resume" ]]; then
  echo overhead > "$root/pipeline.stage"
  bash "$here/run_rate_batch.sh" --stage overhead > "$root/overhead.log" 2>&1
  echo overhead-analysis > "$root/pipeline.stage"
  python -u -B "$here/analyze_rate_batch.py" --stage overhead > "$root/overhead-analysis.log" 2>&1
fi
echo formal > "$root/pipeline.stage"
bash "$here/run_rate_batch.sh" --stage formal > "$root/formal.log" 2>&1
echo analysis > "$root/pipeline.stage"
python -u -B "$here/analyze_rate_batch.py" --stage formal > "$root/formal-analysis.log" 2>&1
echo figures > "$root/pipeline.stage"
python -u -B "$here/plot_rate_batch.py" > "$root/figures.log" 2>&1
echo complete > "$root/pipeline.stage"
