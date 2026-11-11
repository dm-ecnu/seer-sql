#!/usr/bin/env bash
set -euo pipefail

seed=${1:?seed 0-4 required}
shard=${2:?shard 0-11 required}
case "$seed" in 0|1|2|3|4) ;; *) exit 2;; esac
case "$shard" in 0|1|2|3|4|5|6|7|8|9|10|11) ;; *) exit 2;; esac
root=${SEER_ROOT}
export TMPDIR=${TMPDIR}
export SEER_SQLWITNESS_SQLITE_SCRATCH="$TMPDIR"
export SQLITE_TMPDIR="$TMPDIR"
test -d "$TMPDIR"
export XDG_RUNTIME_DIR=/run/user/$(id -u)
tree="$root/stream-worker-wt"
input="$root/datasets/batches/cubes-full-v1"
output="$root/repro/stream-partition-v1/cubes-d100-matrix-v7"
commit=$(git -C "$tree" rev-parse HEAD)
core=$((100 + seed*12 + shard))
methods=(seer_stream seer_stream_sqlwitness sqlwitness sqlwitness_representative)
mkdir -p "$output" "$output/service-logs"
exec 9>"$output/s${seed}-shard${shard}.lock"
flock -n 9
printf 'matrix start %s seed=%s shard=%s core=%s uptime=%s\n' \
  "$(date -Is)" "$seed" "$shard" "$core" "$(uptime)"

verify_result() {
  "$root/venv-seer/bin/python" - "$1" "$commit" "$2" "$3" "$seed" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);commit,batch,method,seed=sys.argv[2:]
c=json.loads((p/'config.json').read_text())
r=json.loads((p/'result.json').read_text());m=r['metrics']
assert c['source_commit']==commit
assert (c['batch'],c['method'],c['seed'])==(batch,method,int(seed))
assert c['method_seed']==0 and c['order']=='shuffle' and c['budget_s']==600
assert r['config_sha256']==hashlib.sha256((p/'config.json').read_bytes()).hexdigest()
assert m['input_queries']==100
assert m['processed_queries']+m['unprocessed_queries']==100
assert len(r['unprocessed_query_ids'])==m['unprocessed_queries']
assert m['invalid_admitted_certificates']==0
assert sum(r['pair_counts'].values())==m['processed_queries']*(m['processed_queries']-1)//2
assert m['censored_at_budget']==(not m['completed_within_budget'])
assert 'late_evidence_rejected' in m
assert 'late_arrivals_rejected' in m
assert all(e['wall_s'] < 600 for e in r['events']
           if e.get('event') in ('arrival','pool_added','evidence_edge_admitted'))
PY
}

index=0
while IFS= read -r batch; do
  if (( index % 12 != shard )); then
    index=$((index+1))
    continue
  fi
  index=$((index+1))
  for offset in 0 1 2 3; do
    method=${methods[$(((seed+shard+offset)%4))]}
    dir="$output/$method/s$seed/$batch"
    if test -f "$dir/result.json"; then
      verify_result "$dir" "$batch" "$method"
      continue
    fi
    test ! -e "$dir" || { echo "incomplete output retained: $dir" >&2; exit 1; }
    mkdir -p "$(dirname "$dir")"
    unit="seer-cubes-v7-${method}-s${seed}-h${shard}-${batch}"
    printf 'unit start %s %s uptime=%s\n' "$(date -Is)" "$unit" "$(uptime)"
    if systemd-run --user --wait --unit "$unit" \
      -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G \
      -p TasksMax=512 -p RuntimeMaxSec=900 \
      -p StandardOutput=append:"$output/service-logs/$unit.log" \
      -p StandardError=append:"$output/service-logs/$unit.log" \
      /bin/bash "$tree/scripts/run_cubes_d100_unit_v7.sh" \
      "$method" "$batch" "$seed" "$dir"; then
      test -f "$dir/result.json" || { echo "missing result: $dir" >&2; exit 1; }
    else
      rc=$?
      mkdir -p "$dir"
      printf '%s\n' "$rc" > "$dir/service-exit.code"
      echo "unit failed, raw output retained: $unit" >&2
      exit "$rc"
    fi
    verify_result "$dir" "$batch" "$method"
    printf 'unit end %s %s uptime=%s\n' "$(date -Is)" "$unit" "$(uptime)"
  done
done < <(cat "$input/cubes_d100.test.ids" "$input/cubes_d100.dev.ids")
printf 'matrix complete %s seed=%s shard=%s uptime=%s\n' \
  "$(date -Is)" "$seed" "$shard" "$(uptime)"
