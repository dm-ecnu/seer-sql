#!/usr/bin/env bash
set -euo pipefail
load=${1:?bird or cubes}
arrival_seed=${2:?arrival seed 0-4}
shard=${3:?shard 0-2}
core=${4:?one exclusive CPU}
case "$load" in bird|cubes) ;; *) exit 2;; esac
case "$arrival_seed" in 0|1|2|3|4) ;; *) exit 2;; esac
case "$shard" in 0|1|2) ;; *) exit 2;; esac
case "$core" in *[!0-9]*|'') exit 2;; esac

base=${SEER_ROOT}
tree="$base/pairseer-multiload-wt"
root="$base/repro/stream-partition-v1/pairseer-${load}-main-v1"
input="$root/input"
test -f "$input/arrival-inputs.json" && test -f "$input/arrivals-s$arrival_seed.jsonl"
mkdir -p "$root/service-logs" "$root/units/s$arrival_seed"
exec 9>"$root/seed-$arrival_seed-shard-$shard.lock"
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

index=0
while IFS= read -r batch; do
  test -n "$batch" || continue
  if (( index % 3 != shard )); then index=$((index+1)); continue; fi
  index=$((index+1))
  output="$root/units/s$arrival_seed/$batch"
  if [[ "$load" == bird && "$arrival_seed" == 0 && "$shard" == 0 && "$batch" == bird-p0135 ]]; then
    test "$(cat "$output/service-exit.code")" = 1
    printf '%s  %s\n' \
      5754e9904bc0133102c3ce7ff54012b8e848c8e35a7393f0a043101603e25366 "$output/config.json" \
      e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 "$output/raw.jsonl" \
      4355a46b19d348dc2f57c046f8ef63d4538ebb936000f3c9ee954a27460dd865 "$output/service-exit.code" \
      d43ab92ffb1f80f10984d936b19eb12c6b4ccba06b3dcc0941f7a0d1aa01e5fd \
      "$root/service-logs/seer-pairseer-bird-v1-s0-h0-bird-p0135.log" | sha256sum -c -
    grep -q 'RuntimeError: owned worker did not terminate' \
      "$root/service-logs/seer-pairseer-bird-v1-s0-h0-bird-p0135.log"
    echo "source method error retained: bird-p0135 seed=0" >&2
    continue
  fi
  if test -f "$output/raw.jsonl"; then
    "$base/venv-seer/bin/python" "$tree/scripts/check_pairseer_multiload_unit_v1.py" "$output"
    continue
  fi
  test ! -e "$output" || { echo "incomplete unit retained: $output" >&2; exit 1; }
  unit="seer-pairseer-${load}-v1-s${arrival_seed}-h${shard}-${batch}"
  echo "unit start $(date -Is) $unit $(uptime)"
  if systemd-run --user --wait --unit "$unit" \
    -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G \
    -p TasksMax=512 -p RuntimeMaxSec=900 \
    -p StandardOutput=append:"$root/service-logs/$unit.log" \
    -p StandardError=append:"$root/service-logs/$unit.log" \
    /bin/bash "$tree/scripts/run_pairseer_multiload_unit_v1.sh" \
    "$load" "$arrival_seed" "$batch" "$output"; then
    test -f "$output/raw.jsonl" || { echo "missing raw result: $output" >&2; exit 1; }
  else
    rc=$?
    mkdir -p "$output"
    printf '%s\n' "$rc" > "$output/service-exit.code"
    echo "unit failed, raw output retained: $unit" >&2
    exit "$rc"
  fi
  "$base/venv-seer/bin/python" "$tree/scripts/check_pairseer_multiload_unit_v1.py" "$output"
  echo "unit end $(date -Is) $unit $(uptime)"
done < "$input/all.ids"
echo "queue complete $(date -Is) load=$load arrival_seed=$arrival_seed shard=$shard"
