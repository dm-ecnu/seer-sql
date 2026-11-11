#!/usr/bin/env bash
# Resume a stalled v9 shard without changing the frozen d4ff8d2a method code.
set -euo pipefail
seed=${1:?seed 0-4}
shard=${2:?shard 0-2}
case "$seed" in 0|1|2|3|4) ;; *) exit 2;; esac
case "$shard" in 0|1|2) ;; *) exit 2;; esac
root=${SEER_ROOT}
source_tree="$root/stream-worker-progress-wt"
input="$root/datasets/batches/leetcode-scale-v1"
output="$root/repro/stream-partition-v1/leetcode-scale-v9"
expected_commit=d4ff8d2a4d4d83c8f4dd6cd6a72f686605d2b4a7
core=$((254 + seed*3 + shard))
methods=(seer_stream seer_stream_sqlwitness sqlwitness sqlwitness_representative)
export TMPDIR=${TMPDIR}
export SQLITE_TMPDIR="$TMPDIR"
export SEER_SQLWITNESS_SQLITE_SCRATCH="$TMPDIR"
export XDG_RUNTIME_DIR=/run/user/$(id -u)
test -d "$TMPDIR"
test "$(git -C "$source_tree" rev-parse HEAD)" = "$expected_commit"
mkdir -p "$output/service-logs"
exec 9>"$output/seed-$seed-shard-$shard.lock"
flock -n 9
verify() {
 "$root/venv-seer/bin/python" - "$1" "$expected_commit" "$2" "$3" "$seed" "$core" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]); expected,batch,method,seed,core=sys.argv[2:]
c=json.loads((p/'config.json').read_text()); r=json.loads((p/'result.json').read_text()); m=r['metrics']
assert c['source_commit']==expected
assert (c['batch'],c['method'],c['seed'])==(batch,method,int(seed))
assert c['method_seed']==0 and c['order']=='shuffle' and c['budget_s']==600
assert c['affinity']==[int(core)]
assert r['config_sha256']==hashlib.sha256((p/'config.json').read_bytes()).hexdigest()
assert m['invalid_admitted_certificates']==0
assert m['processed_queries']+m['unprocessed_queries']==m['input_queries']==len(c['query_ids'])
assert len(r['unprocessed_query_ids'])==m['unprocessed_queries']
assert sum(r['pair_counts'].values())==m['processed_queries']*(m['processed_queries']-1)//2
assert m['censored_at_budget']==(not m['completed_within_budget'])
assert all(e['wall_s']<600 for e in r['events'] if e.get('event') in ('arrival','pool_added','evidence_edge_admitted'))
PY
}
index=0
while IFS= read -r batch; do
 test -n "$batch" || continue
 if (( index % 3 != shard )); then index=$((index+1)); continue; fi
 index=$((index+1))
 for offset in 0 1 2 3; do
  method=${methods[$(((seed + offset) % 4))]}
  dir="$output/$method/s$seed/$batch"
  if test -f "$dir/result.json"; then verify "$dir" "$batch" "$method"; continue; fi
  test ! -e "$dir" || { echo "incomplete original retained: $dir" >&2; exit 1; }
  mkdir -p "$(dirname "$dir")"
  unit="seer-lcstream-v9-${method}-s${seed}-${batch}"
  log="$output/service-logs/$unit.log"
  printf 'resume unit start %s %s\n' "$(date -Is)" "$unit"
  if ! systemd-run --user --unit "$unit" \
       -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G \
       -p TasksMax=512 -p RuntimeMaxSec=900 \
       -p StandardOutput=append:"$log" -p StandardError=append:"$log" \
       /bin/bash "$source_tree/scripts/run_stream_scale_unit_v9.sh" \
       "$method" "$batch" "$seed" "$dir"; then
    printf '1\n' > "$dir/service-exit.code"
    echo "launch failed; raw directory retained: $unit" >&2
    exit 1
  fi
  seconds=0
  inactive=0
  while true; do
    state=$(systemctl --user show "$unit.service" -p ActiveState --value 2>/dev/null || true)
    if [[ "$state" == active || "$state" == activating ]]; then
      inactive=0
    else
      inactive=$((inactive+2))
      if test -f "$dir/result.json" && (( inactive >= 2 )); then break; fi
      if (( inactive >= 30 )); then
        printf '1\n' > "$dir/service-exit.code"
        echo "service ended without result: $unit" >&2
        exit 1
      fi
    fi
    if (( seconds >= 930 )); then
      echo "service exceeded 930 s; retaining active unit: $unit" >&2
      exit 1
    fi
    sleep 2
    seconds=$((seconds+2))
  done
  verify "$dir" "$batch" "$method"
  printf 'resume unit end %s %s\n' "$(date -Is)" "$unit"
 done
done < <(cat "$input/test.ids" "$input/dev.ids")
printf 'resume shard complete %s seed=%s shard=%s\n' "$(date -Is)" "$seed" "$shard"
