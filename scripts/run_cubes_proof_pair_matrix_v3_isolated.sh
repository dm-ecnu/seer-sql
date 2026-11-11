#!/usr/bin/env bash
set -euo pipefail
seed=${1:?seed 0-4 required}
shard=${2:?shard 0-9 required}
core=${3:?exclusive CPU 0-383 required}
case "$seed" in 0|1|2|3|4) ;; *) exit 2;; esac
case "$shard" in 0|1|2|3|4|5|6|7|8|9) ;; *) exit 2;; esac
[[ "$core" =~ ^[0-9]+$ ]] && (( core < 384 ))
(( core < 169 || core > 185 ))
(( core < 288 || core > 291 ))
root=${SEER_ROOT}
tree="$root/stream-proof-pair-once-wt"
input="$root/datasets/batches/cubes-full-v1"
output="$root/repro/stream-partition-v1/cubes-proof-canonical-v3-isolated"
commit=113c3920997e76d189e9dadfccc78871e97188fa
test "$(git -C "$tree" rev-parse HEAD)" = "$commit"
test -z "$(git -C "$tree" status --porcelain)"
export TMPDIR=${TMPDIR}
export SQLITE_TMPDIR="$TMPDIR"
export SEER_SQLWITNESS_SQLITE_SCRATCH="$TMPDIR"
test -d "$TMPDIR"
export XDG_RUNTIME_DIR=/run/user/$(id -u)
mkdir -p "$output/service-logs"
exec 9>"$output/s${seed}-h${shard}.lock"
flock -n 9

verify_result() {
  "$root/venv-seer/bin/python" - "$1" "$commit" "$2" "$seed" "$core" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);commit,batch,seed,core=sys.argv[2:]
c=json.loads((p/'config.json').read_text())
r=json.loads((p/'result.json').read_text());m=r['metrics']
assert c['source_commit']==commit
assert (c['batch'],c['method'],c['seed'])==(batch,'polygon_canonical_pair',int(seed))
assert c['method_seed']==0 and c['order']=='shuffle' and c['budget_s']==600
assert c['affinity']==[int(core)]
assert c['temporary_directory']=='${TMPDIR}'
assert r['config_sha256']==hashlib.sha256((p/'config.json').read_bytes()).hexdigest()
assert m['input_queries']==100
assert m['processed_queries']+m['unprocessed_queries']==100
assert len(r['unprocessed_query_ids'])==m['unprocessed_queries']
assert m['invalid_admitted_certificates']==0
assert sum(r['pair_counts'].values())==m['processed_queries']*(m['processed_queries']-1)//2
assert m['censored_at_budget']==(not m['completed_within_budget'])
assert len(r['pair_attempts'])==m['pair_decision_requests']
assert m['full_pair_targets']==m['processed_queries']*(m['processed_queries']-1)//2
assert all(e['wall_s']<600 for e in r['events']
           if e.get('event') in ('arrival','pool_added','evidence_edge_admitted'))
PY
}

i=0
while IFS= read -r batch; do
  if (( i % 10 != shard )); then i=$((i+1)); continue; fi
  i=$((i+1))
  dir="$output/polygon_canonical_pair/s$seed/$batch"
  if test -f "$dir/result.json"; then
    verify_result "$dir" "$batch"
    continue
  fi
  test ! -e "$dir" || { echo "incomplete output retained: $dir" >&2; exit 1; }
  mkdir -p "$(dirname "$dir")"
  unit="seer-cubes-proof-v3iso-s${seed}-h${shard}-${batch}"
  printf 'unit start %s %s uptime=%s\n' "$(date -Is)" "$unit" "$(uptime)"
  if systemd-run --user --wait --unit "$unit" \
    -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G \
    -p TasksMax=512 -p RuntimeMaxSec=900 \
    -p StandardOutput=append:"$output/service-logs/$unit.log" \
    -p StandardError=append:"$output/service-logs/$unit.log" \
    /bin/bash "$tree/scripts/run_cubes_proof_pair_unit_v2.sh" \
    "$batch" "$seed" "$dir"; then
    test -f "$dir/result.json" || { echo "missing result: $dir" >&2; exit 1; }
  else
    rc=$?
    mkdir -p "$dir"
    printf '%s\n' "$rc" > "$dir/service-exit.code"
    echo "unit failed, raw output retained: $unit" >&2
    exit "$rc"
  fi
  verify_result "$dir" "$batch"
  printf 'unit end %s %s uptime=%s\n' "$(date -Is)" "$unit" "$(uptime)"
done < <(cat "$input/cubes_d100.test.ids" "$input/cubes_d100.dev.ids")
printf 'matrix complete %s seed=%s shard=%s uptime=%s\n' \
  "$(date -Is)" "$seed" "$shard" "$(uptime)"
