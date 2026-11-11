#!/bin/bash
set -euo pipefail
lane=${1:?lane}
case "$lane" in 0|1|2) ;; *) exit 2;; esac
B=${SEER_ROOT}
W=$B/ablation-scale-wt
D=$B/datasets/batches/leetcode-scale-v1
O=$B/leetcode-ablation-scale-v1
. "$B/env2.sh"
export PYTHONPATH="$W/src:$W/eval:$W/scripts" OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
cd "$W"
exec 9>"$O/lane$lane.lock"
flock -n 9
core=$((184+lane));variants=(full no_deterministic_query_memo no_hard_sigma)
echo "matrix start $(date -Is) lane=$lane core=$core"
for seed in 0 1 2 3 4; do
 index=0
 while read -r bid; do
  variant=${variants[$(((lane+seed+index)%3))]}
  index=$((index+1))
  ../venv-seer/bin/python scripts/leetcode_ablation_protocol.py check
  output=$O/$variant/s$seed/$bid
  if [ -f "$output/done.json" ]; then
   ../venv-seer/bin/python scripts/leetcode_ablation_protocol.py finish "$variant" "$bid" "$seed"
   continue
  fi
  test ! -e "$output"
  mkdir -p "$(dirname "$output")"
  unit=seer-lcablation-v1-l$lane-s$seed-$bid
  echo "unit start $(date -Is) $variant $unit"
  if systemd-run --user --wait --pipe --unit "$unit" -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G -p TasksMax=128 -p RuntimeMaxSec=240 /bin/bash "$W/scripts/run_leetcode_ablation_unit.sh" "$variant" "$bid" "$seed" "$output"; then
   ../venv-seer/bin/python scripts/check_leetcode_scale_unit.py "$output" > "$output/check.log" 2>&1
   ../venv-seer/bin/python scripts/leetcode_ablation_protocol.py finish "$variant" "$bid" "$seed"
  else
   rc=$?; printf '%s\n' "$rc" > "$output/service-exit.code"
   echo "unit failed; retained artifacts; no retry $unit" >&2
   exit "$rc"
  fi
  echo "unit end $(date -Is) $variant $unit"
 done < "$D/test.ids"
done
echo "matrix independently replayed all units $(date -Is) lane=$lane"
