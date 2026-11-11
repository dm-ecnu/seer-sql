#!/usr/bin/env bash
set -euo pipefail
arrival_seed=${1:?arrival seed 0-4}
core=${2:?one exclusive CPU}
case "$arrival_seed" in 0|1|2|3|4) ;; *) exit 2;; esac
case "$core" in *[!0-9]*|'') exit 2;; esac

base=${SEER_ROOT}
tree="$base/stream-scale-score-v10-wt"
root="$base/repro/stream-partition-v1/pairseer-leetcode-main-v1"
input="$root/input"
test -f "$input/arrival-inputs.json" && test -f "$input/arrivals-s$arrival_seed.jsonl"
mkdir -p "$root/service-logs" "$root/units/s$arrival_seed"
exec 9>"$root/seed-$arrival_seed.lock"
flock -n 9
current_commit=$(git -C "$tree" rev-parse HEAD)
exec 8>"$root/launcher-commit.lock"
flock 8
if test -f "$root/launcher-commit.txt"; then
  test "$(cat "$root/launcher-commit.txt")" = "$current_commit"
else
  printf '%s\n' "$current_commit" > "$root/launcher-commit.txt"
fi
flock -u 8
export XDG_RUNTIME_DIR=/run/user/$(id -u)

while IFS= read -r batch; do
  test -n "$batch" || continue
  output="$root/units/s$arrival_seed/$batch"
  if test -f "$output/raw.jsonl"; then
    "$base/venv-seer/bin/python" "$tree/scripts/check_pairseer_leetcode_unit_v1.py" "$output"
    continue
  fi
  test ! -e "$output" || { echo "incomplete unit retained: $output" >&2; exit 1; }
  unit="seer-pairseer-lc-v1-s${arrival_seed}-${batch}"
  echo "unit start $(date -Is) $unit $(uptime)"
  if systemd-run --user --wait --unit "$unit" \
    -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G \
    -p TasksMax=512 -p RuntimeMaxSec=900 \
    -p StandardOutput=append:"$root/service-logs/$unit.log" \
    -p StandardError=append:"$root/service-logs/$unit.log" \
    /bin/bash "$tree/scripts/run_pairseer_leetcode_unit_v1.sh" \
    "$arrival_seed" "$batch" "$output"; then
    test -f "$output/raw.jsonl" || { echo "missing raw result: $output" >&2; exit 1; }
  else
    rc=$?
    printf '%s\n' "$rc" > "$output/service-exit.code"
    echo "unit failed, raw output retained: $unit" >&2
    exit "$rc"
  fi
  "$base/venv-seer/bin/python" "$tree/scripts/check_pairseer_leetcode_unit_v1.py" "$output"
  echo "unit end $(date -Is) $unit $(uptime)"
done < "$input/test-n500.ids"
echo "seed complete $(date -Is) arrival_seed=$arrival_seed"
