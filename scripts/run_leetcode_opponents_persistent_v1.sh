#!/usr/bin/env bash
set -euo pipefail
seed=${1:?seed 0-4 required}
shard=${2:?shard 0-2 required}
case "$seed" in 0|1|2|3|4) ;; *) exit 2;; esac
case "$shard" in 0|1|2) ;; *) exit 2;; esac
root=${SEER_ROOT}
tree="$root/seer-persistent-native-wt"
input="$root/datasets/batches/leetcode-scale-v1"
output="$root/repro/stream-partition-v1/leetcode-opponents-persistent-v1"
expected=$(git -C "$tree" rev-parse HEAD)
test "$(git -C "$tree" rev-parse HEAD)" = "$expected"
test -z "$(git -C "$tree" status --porcelain)"
export TMPDIR=${TMPDIR}
export SQLITE_TMPDIR="$TMPDIR" SEER_SQLWITNESS_SQLITE_SCRATCH="$TMPDIR"
export XDG_RUNTIME_DIR=/run/user/$(id -u)
test -d "$TMPDIR"
core=$((120 + seed*3 + shard))
methods=(parseval xdatan)
mkdir -p "$output/service-logs"
exec 9>"$output/s${seed}-h${shard}.lock"
flock -n 9
while systemctl --user list-units --plain --no-legend --all \
  'seer-sw-official-bird-s*-h*.service' | grep -q ' active '; do
  sleep 30
done

verify_result() {
  "$root/venv-seer/bin/python" - "$1" "$2" "$3" "$seed" "$expected" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);batch,method,seed,expected=sys.argv[2:]
c=json.loads((p/'config.json').read_text())
r=json.loads((p/'result.json').read_text());m=r['metrics']
n=int(batch.rsplit('-n',1)[1]);assert n in (10,25,50,100,200,500)
assert c['source_commit']==expected
assert (c['batch'],c['method'],c['seed'])==(batch,method,int(seed))
assert c['method_seed']==0 and c['order']=='shuffle' and c['budget_s']==600
assert c['temporary_directory']=='${TMPDIR}'
assert len(c['query_ids'])==m['input_queries']==n
assert r['config_sha256']==hashlib.sha256((p/'config.json').read_bytes()).hexdigest()
assert m['processed_queries']+m['unprocessed_queries']==n
assert len(r['unprocessed_query_ids'])==m['unprocessed_queries']
assert sum(r['pair_counts'].values())==m['processed_queries']*(m['processed_queries']-1)//2
assert m['invalid_admitted_certificates']==0
assert m['censored_at_budget']==(not m['completed_within_budget'])
if method=='parseval':
    assert m['native_worker_starts']<=1
if method=='xdatan':
    assert m['native_batch_calls']==len(r['calls'])
assert all(e['wall_s']<600 for e in r['events']
           if e.get('event') in ('arrival','pool_added','evidence_edge_admitted'))
PY
}

i=0
while IFS= read -r batch; do
  test -n "$batch" || continue
  if (( i % 3 != shard )); then i=$((i+1)); continue; fi
  i=$((i+1))
  for offset in 0 1; do
    method=${methods[$(((seed+shard+offset)%2))]}
    dir="$output/$method/s$seed/$batch"
    if test -f "$dir/result.json"; then
      verify_result "$dir" "$batch" "$method"
      continue
    fi
    test ! -e "$dir" || { echo "incomplete output retained: $dir" >&2; exit 1; }
    mkdir -p "$(dirname "$dir")"
    unit="seer-lc-opp-persist-v1-${method}-s${seed}-${batch}"
    printf 'unit start %s %s core=%s uptime=%s\n' "$(date -Is)" "$unit" "$core" "$(uptime)"
    if systemd-run --user --wait --unit "$unit" \
      -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G \
      -p TasksMax=512 -p RuntimeMaxSec=900 \
      -p StandardOutput=append:"$output/service-logs/$unit.log" \
      -p StandardError=append:"$output/service-logs/$unit.log" \
      /bin/bash "$tree/scripts/run_leetcode_opponents_persistent_unit_v1.sh" \
      "$method" "$batch" "$seed" "$dir"; then
      test -f "$dir/result.json" || { echo "missing result: $dir" >&2; exit 1; }
    else
      rc=$?
      printf '%s\n' "$rc" > "$dir/service-exit.code"
      echo "unit failed, raw output retained: $unit" >&2
      exit "$rc"
    fi
    verify_result "$dir" "$batch" "$method"
    printf 'unit end %s %s uptime=%s\n' "$(date -Is)" "$unit" "$(uptime)"
  done
done < <(cat "$input/test.ids" "$input/dev.ids")
printf 'matrix complete %s seed=%s shard=%s\n' "$(date -Is)" "$seed" "$shard"
