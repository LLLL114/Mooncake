#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source /root/miniconda3/etc/profile.d/conda.sh
conda activate mooncake
export PYTHONDONTWRITEBYTECODE=1
root=/root/mooncake-tent-multirdma-output/cross-node-multirail/oscillation-20260917
mkdir -p "$root/logs"
if [[ -e "$root/logs/formal-measurement.log" || -e "$root/pipeline.exit" ]]; then
  printf 'Pipeline already started; inspect saved state and resume the necessary stage explicitly.\n' >&2
  exit 1
fi
[[ -f "$root/overhead-oscillation-analysis.json" ]]
trap 'rc=$?; printf "finished %s\n" "$rc" > "$root/pipeline.exit"' EXIT
printf 'formal measurement\n' > "$root/pipeline.stage"
bash "$here/run_oscillation.sh" --stage formal > "$root/logs/formal-measurement.log" 2>&1
printf 'formal analysis\n' > "$root/pipeline.stage"
python -u -B "$here/analyze_oscillation.py" --stage formal > "$root/logs/formal-analysis.log" 2>&1
printf 'figures\n' > "$root/pipeline.stage"
python -u -B "$here/plot_oscillation.py" > "$root/logs/figures.log" 2>&1
printf 'complete\n' > "$root/pipeline.stage"
