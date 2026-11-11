#!/bin/bash
set -euo pipefail
method=${1:?method required}
case "$method" in pair_seer) core=168;; sqlwitness) core=169;; parseval) core=170;; xdatan) core=171;; *) exit 2;; esac
B=${SEER_ROOT}
W=$B/duckdb-worker-wt
O=$B/leetcode-scale-v3
D=$B/datasets/batches/leetcode-scale-v1
export XDG_RUNTIME_DIR=/run/user/$(id -u)
. "$B/env2.sh"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 ARROW_DEFAULT_MEMORY_POOL=system
ulimit -v 8388608
cd "$W"
exec 9>"$O/$method.lock"
flock -n 9
echo "matrix start $(date -Is) $(uptime) method=$method core=$core"
for seed in 0 1 2 3 4; do
 while read -r bid; do
  ../venv-seer/bin/python scripts/leetcode_scale_protocol.py check
  output=$O/$method/s$seed/$bid
  if [ -f "$output/done.json" ]; then
   ../venv-seer/bin/python scripts/leetcode_scale_protocol.py finish "$method" "$bid" "$seed"
   continue
  fi
  test ! -e "$output"
  mkdir -p "$(dirname "$output")"
  unit=seer-lcscale-v3-$method-s$seed-$bid
  echo "unit start $(date -Is) $unit $(uptime)"
  if systemd-run --user --wait --pipe --unit "$unit" -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G -p TasksMax=128 -p RuntimeMaxSec=240 /bin/bash "$W/scripts/run_leetcode_scale_unit.sh" "$method" "$bid" "$seed" "$output"; then
   # Offline replay is outside the measured leaf service.
   ../venv-seer/bin/python scripts/check_leetcode_scale_unit.py "$output" > "$output/check.log" 2>&1
   ../venv-seer/bin/python scripts/leetcode_scale_protocol.py finish "$method" "$bid" "$seed"
  else
   rc=$?; printf '%s\n' "$rc" > "$output/service-exit.code"
   echo "unit failed; retained artifacts; no retry $unit" >&2
   exit "$rc"
  fi
  echo "unit end $(date -Is) $unit $(uptime)"
 done < "$D/test.ids"
done
echo "matrix execution and independent replay complete $(date -Is) method=$method $(uptime)"
