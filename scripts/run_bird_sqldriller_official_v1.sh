#!/usr/bin/env bash
set -euo pipefail
seed=${1:?arrival seed 0-4}
shard=${2:?shard 0-2}
core=${3:?exclusive CPU}
case "$seed" in 0|1|2|3|4) ;; *) exit 2;; esac
case "$shard" in 0|1|2) ;; *) exit 2;; esac
case "$core" in *[!0-9]*|'') exit 2;; esac

base=${SEER_ROOT}
tree="$base/stream-worker-progress-wt"
data="$base/datasets/batches/public-batch-v1"
output="$base/repro/stream-partition-v1/bird-sqldriller-official-v1"
expected_commit=d4ff8d2a4d4d83c8f4dd6cd6a72f686605d2b4a7
test "$(git -C "$tree" rev-parse HEAD)" = "$expected_commit"
test -d ${TMPDIR}
mkdir -p "$output/service-logs"
exec 9>"$output/s${seed}-h${shard}.lock"
flock -n 9
export XDG_RUNTIME_DIR=/run/user/$(id -u)

verify_result() {
  "$base/venv-seer/bin/python" - "$1" "$2" "$seed" "$expected_commit" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);bid,seed,commit=sys.argv[2:]
c=json.loads((p/'config.json').read_text());r=json.loads((p/'result.json').read_text());m=r['metrics']
assert c['source_commit']==commit
assert (c['batch'],c['method'],c['seed'])==(bid,'sqldriller_official',int(seed))
assert c['method_seed']==0 and c['order']=='shuffle' and c['budget_s']==600
assert c['temporary_directory']=='${TMPDIR}'
assert len(c['affinity'])==1
assert r['config_sha256']==hashlib.sha256((p/'config.json').read_bytes()).hexdigest()
assert m['input_queries']==11 and m['processed_queries']+m['unprocessed_queries']==11
assert m['invalid_admitted_certificates']==0
assert sum(r['pair_counts'].values())==m['processed_queries']*(m['processed_queries']-1)//2
assert m['censored_at_budget']==(not m['completed_within_budget'])
assert all(e['wall_s']<600 for e in r['events'] if e.get('event') in
           ('arrival','pool_added','evidence_edge_admitted'))
for call in r['calls']:
    native=call['native']
    if native.get('implementation'):
        assert native['implementation']=='official'
        assert native.get('upstream_commit')=='651401923af77db68f7f32b060f57da1b8e1e444'
    else:
        assert native['verdict'] in ('timeout','error')
print(json.dumps({'batch':bid,'seed':int(seed),'processed':m['processed_queries'],
                  'separated':r['pair_counts']['separated'],'official_calls':len(r['calls']),
                  'unsupported':sum(call['native']['verdict']=='unsupported' for call in r['calls']),
                  'censored':m['censored_at_budget']},sort_keys=True))
PY
}

index=0
while IFS= read -r batch; do
  test -n "$batch" || continue
  if (( index % 3 != shard )); then index=$((index+1)); continue; fi
  index=$((index+1))
  dir="$output/sqldriller_official/s$seed/$batch"
  if test -f "$dir/result.json"; then verify_result "$dir" "$batch"; continue; fi
  test ! -e "$dir" || { echo "incomplete unit retained: $dir" >&2; exit 1; }
  mkdir -p "$(dirname "$dir")"
  unit="seer-bird-sqld-v1-s${seed}-h${shard}-${batch}"
  echo "unit start $(date -Is) $unit $(uptime)"
  if systemd-run --user --wait --unit "$unit" \
    -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G \
    -p TasksMax=512 -p RuntimeMaxSec=900 \
    -p StandardOutput=append:"$output/service-logs/$unit.log" \
    -p StandardError=append:"$output/service-logs/$unit.log" \
    /bin/bash "$tree/scripts/run_bird_public_unit_v7.sh" \
    sqldriller_official "$batch" "$seed" "$dir"; then
    test -f "$dir/result.json" || { echo "missing result: $dir" >&2; exit 1; }
  else
    rc=$?; mkdir -p "$dir"; printf '%s\n' "$rc" > "$dir/service-exit.code"
    echo "unit failed, raw output retained: $unit" >&2; exit "$rc"
  fi
  verify_result "$dir" "$batch"
  echo "unit end $(date -Is) $unit $(uptime)"
done < <(cat "$data/test.ids" "$data/dev.ids")
echo "shard complete $(date -Is) seed=$seed shard=$shard"
