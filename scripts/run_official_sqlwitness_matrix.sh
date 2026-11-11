#!/usr/bin/env bash
set -euo pipefail
dataset=${1:?dataset jsonl}
ids=${2:?ordered batch id files, colon-separated when multiple}
output=${3:?output root}
seed=${4:?seed 0-4}
shard=${5:?shard index}
shards=${6:?shard count}
core=${7:?exclusive CPU}
case "$seed" in 0|1|2|3|4) ;; *) exit 2;; esac
[[ "$shard" =~ ^[0-9]+$ && "$shards" =~ ^[0-9]+$ && "$core" =~ ^[0-9]+$ ]]
(( shards > 0 && shard < shards ))
root=${SEER_ROOT}
tree="$root/sqlwitness-official-batch-wt"
expected_commit=$(git -C "$tree" rev-parse HEAD)
IFS=: read -r -a id_files <<< "$ids"
for file in "${id_files[@]}"; do test -f "$file"; done
export XDG_RUNTIME_DIR=/run/user/$(id -u)
mkdir -p "$output/service-logs"
exec 9>"$output/s${seed}-h${shard}.lock"
flock -n 9

verify_result() {
  "$root/venv-seer/bin/python" - "$1" "$expected_commit" "$2" "$seed" <<'PY'
import csv, hashlib, json, pathlib, sys
p = pathlib.Path(sys.argv[1]); commit, batch, seed = sys.argv[2:]
c = json.loads((p / 'config.json').read_text())
r = json.loads((p / 'result.json').read_text()); m = r['metrics']
assert c['source_commit'] == commit
assert (c['method'], c['batch'], c['seed'], c['order'], c['method_seed']) == (
    'sqlwitness', batch, int(seed), 'shuffle', 0)
assert c['budget_s'] == 600 and c['native_mode'] == 'official_experiment_driver_one_process_per_batch'
assert r['config_sha256'] == hashlib.sha256((p / 'config.json').read_bytes()).hexdigest()
assert m['invalid_admitted_certificates'] == 0
assert m['processed_queries'] + m['unprocessed_queries'] == m['input_queries'] == len(c['query_ids'])
assert sum(r['pair_counts'].values()) == m['processed_queries'] * (m['processed_queries'] - 1) // 2
assert m['censored_at_budget'] == (not m['completed_within_budget'])
assert r['official_driver_status'] in ('exit_0', 'budget_terminated', 'not_started')
assert r['official_driver_requested_pairs'] >= m['native_pair_calls'] == len(r['calls'])
if (p / 'official-batch.csv').exists():
    with (p / 'official-batch.csv').open(newline='') as f:
        assert len(list(csv.DictReader(f))) == len(r['calls'])
assert all(e['wall_s'] < 600 for e in r['events']
           if e.get('event') in ('arrival', 'pool_added', 'evidence_edge_admitted'))
PY
}

index=0
while IFS= read -r batch; do
  test -n "$batch" || continue
  if (( index % shards != shard )); then
    index=$((index+1)); continue
  fi
  index=$((index+1))
  dir="$output/sqlwitness/s$seed/$batch"
  if test -f "$dir/result.json"; then
    verify_result "$dir" "$batch"
    continue
  fi
  test ! -e "$dir" || { echo "incomplete output retained: $dir" >&2; exit 1; }
  mkdir -p "$(dirname "$dir")"
  unit="seer-sw-official-s${seed}-h${shard}-${batch}"
  echo "start $(date -Is) $unit core=$core source=$expected_commit"
  if systemd-run --user --wait --unit "$unit" \
      -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G \
      -p TasksMax=512 -p RuntimeMaxSec=900 \
      -p StandardOutput=append:"$output/service-logs/$unit.log" \
      -p StandardError=append:"$output/service-logs/$unit.log" \
      /bin/bash "$tree/scripts/run_official_sqlwitness_unit.sh" \
      "$dataset" "$batch" "$seed" "$dir"; then
    test -f "$dir/result.json" || { echo "missing result: $dir" >&2; exit 1; }
  else
    rc=$?
    test -d "$dir" && printf '%s\n' "$rc" > "$dir/service-exit.code"
    echo "unit failed, raw output retained: $unit" >&2
    exit "$rc"
  fi
  verify_result "$dir" "$batch"
  echo "end $(date -Is) $unit"
done < <(cat "${id_files[@]}")
