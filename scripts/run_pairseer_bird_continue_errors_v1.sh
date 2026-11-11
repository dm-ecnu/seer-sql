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
test "$load" = bird

verified_method_error() {
  "$base/venv-seer/bin/python" - "$1" "$2" "$3" "$arrival_seed" "$core" "$current_commit" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]); log=pathlib.Path(sys.argv[2])
batch,seed,core,aux=sys.argv[3:]
assert (p/'service-exit.code').read_text().strip()=='1'
assert (p/'raw.jsonl').is_file() and (p/'raw.jsonl').stat().st_size==0
c=json.loads((p/'config.json').read_text())
assert (c['load'],c['method'],c['batch'],c['arrival_seed'],c['affinity'],
        c['budget_s'],c['source_commit'],c['auxiliary_commit'])==(
        'bird','pair_seer',batch,int(seed),[int(core)],600,
        'd4ff8d2a4d4d83c8f4dd6cd6a72f686605d2b4a7',aux)
assert 'RuntimeError: owned worker did not terminate' in log.read_text()
print(json.dumps({'source_method_error':batch,'seed':int(seed),
                  'config_sha256':hashlib.sha256((p/'config.json').read_bytes()).hexdigest(),
                  'empty_raw_sha256':hashlib.sha256((p/'raw.jsonl').read_bytes()).hexdigest(),
                  'exit_sha256':hashlib.sha256((p/'service-exit.code').read_bytes()).hexdigest(),
                  'service_log_sha256':hashlib.sha256(log.read_bytes()).hexdigest()},sort_keys=True))
PY
}

index=0
while IFS= read -r batch; do
  test -n "$batch" || continue
  if (( index % 3 != shard )); then index=$((index+1)); continue; fi
  index=$((index+1))
  output="$root/units/s$arrival_seed/$batch"
  unit="seer-pairseer-${load}-v1-s${arrival_seed}-h${shard}-${batch}"
  log="$root/service-logs/$unit.log"
  if test -s "$output/raw.jsonl"; then
    test ! -e "$output/service-exit.code"
    "$base/venv-seer/bin/python" "$tree/scripts/check_pairseer_multiload_unit_v1.py" "$output"
    continue
  fi
  if test -e "$output" && verified_method_error "$output" "$log" "$batch"; then
    continue
  fi
  test ! -e "$output" || { echo "incomplete unit retained: $output" >&2; exit 1; }
  echo "unit start $(date -Is) $unit $(uptime)"
  if systemd-run --user --wait --unit "$unit" \
    -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G \
    -p TasksMax=512 -p RuntimeMaxSec=900 \
    -p StandardOutput=append:"$log" \
    -p StandardError=append:"$log" \
    /bin/bash "$tree/scripts/run_pairseer_multiload_unit_v1.sh" \
    "$load" "$arrival_seed" "$batch" "$output"; then
    test -f "$output/raw.jsonl" || { echo "missing raw result: $output" >&2; exit 1; }
  else
    rc=$?
    mkdir -p "$output"
    printf '%s\n' "$rc" > "$output/service-exit.code"
    if verified_method_error "$output" "$log" "$batch"; then
      continue
    fi
    echo "unclassified unit failure, raw output retained: $unit" >&2
    exit "$rc"
  fi
  "$base/venv-seer/bin/python" "$tree/scripts/check_pairseer_multiload_unit_v1.py" "$output"
  echo "unit end $(date -Is) $unit $(uptime)"
done < "$input/all.ids"
echo "queue complete $(date -Is) load=$load arrival_seed=$arrival_seed shard=$shard"
