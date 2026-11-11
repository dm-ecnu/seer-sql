#!/usr/bin/env bash
set -euo pipefail
root=${SEER_ROOT}
source_tree="$root/stream-worker-progress-wt"
original="$root/repro/stream-partition-v1/leetcode-scale-v9"
supplement="$root/repro/stream-partition-v1/leetcode-scale-v9-supplement-v1"
input="$root/datasets/batches/leetcode-scale-v1"
expected_commit=d4ff8d2a4d4d83c8f4dd6cd6a72f686605d2b4a7
seed=2
shard=2
core=262
methods=(seer_stream seer_stream_sqlwitness sqlwitness sqlwitness_representative)
export TMPDIR=${TMPDIR}
export SEER_SQLWITNESS_SQLITE_SCRATCH="$TMPDIR"
export SQLITE_TMPDIR="$TMPDIR"
test -d "$TMPDIR"
test "$(git -C "$source_tree" rev-parse HEAD)" = "$expected_commit"
failed="$original/sqlwitness/s2/leetcode-1132-n50"
test ! -f "$failed/result.json"
printf '%s  %s\n' \
 '4336839b6d27f210fab77ffad6fef35f5dd3c11f2a9102800a615de3166106a0' "$failed/config.json" \
 '12a80f29ddc94d05aeeaa630cb29fef523efddaa8587577ae764a1a60f7c7bd2' "$failed/entry-metrics.jsonl" \
 '4355a46b19d348dc2f57c046f8ef63d4538ebb936000f3c9ee954a27460dd865' "$failed/service-exit.code" | sha256sum -c -
mkdir -p "$supplement/service-logs"
exec 9>"$supplement/seed-2-shard-2.lock"
flock -n 9
verify() {
 "$root/venv-seer/bin/python" - "$1" "$expected_commit" "$2" "$3" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]); expected,batch,method=sys.argv[2:]
c=json.loads((p/'config.json').read_text()); r=json.loads((p/'result.json').read_text()); m=r['metrics']
assert c['source_commit']==expected
assert (c['batch'],c['method'],c['seed'])==(batch,method,2)
assert c['method_seed']==0 and c['order']=='shuffle' and c['budget_s']==600
assert c['affinity']==[262]
assert r['config_sha256']==hashlib.sha256((p/'config.json').read_bytes()).hexdigest()
assert m['invalid_admitted_certificates']==0
assert m['processed_queries']+m['unprocessed_queries']==m['input_queries']==len(c['query_ids'])
assert len(r['unprocessed_query_ids'])==m['unprocessed_queries']
assert sum(r['pair_counts'].values())==m['processed_queries']*(m['processed_queries']-1)//2
assert m['censored_at_budget'] == (not m['completed_within_budget'])
PY
}
index=0
while IFS= read -r batch; do
 test -n "$batch" || continue
 if (( index % 3 != shard )); then index=$((index+1)); continue; fi
 index=$((index+1))
 for offset in 0 1 2 3; do
  method=${methods[$(((seed + offset) % 4))]}
  source_dir="$original/$method/s$seed/$batch"
  if test -f "$source_dir/result.json"; then verify "$source_dir" "$batch" "$method"; continue; fi
  if test -e "$source_dir" && test ! -f "$source_dir/service-exit.code"; then
   echo "unexpected incomplete original: $source_dir" >&2; exit 1
  fi
  if test -f "$source_dir/service-exit.code" &&
     { test "$method" != sqlwitness || test "$batch" != leetcode-1132-n50; }; then
   echo "unreviewed original failure: $source_dir" >&2; exit 1
  fi
  dir="$supplement/$method/s$seed/$batch"
  if test -f "$dir/result.json"; then verify "$dir" "$batch" "$method"; continue; fi
  test ! -e "$dir" || { echo "incomplete supplement retained: $dir" >&2; exit 1; }
  mkdir -p "$(dirname "$dir")"
  unit="seer-lcstream-v9supp-${method}-s${seed}-${batch}"
  printf 'supplement start %s %s\n' "$(date -Is)" "$unit"
  if systemd-run --user --wait --unit "$unit" \
      -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G \
      -p TasksMax=512 -p RuntimeMaxSec=900 \
      -p StandardOutput=append:"$supplement/service-logs/$unit.log" \
      -p StandardError=append:"$supplement/service-logs/$unit.log" \
      /bin/bash "$source_tree/scripts/run_stream_scale_unit_v9.sh" \
      "$method" "$batch" "$seed" "$dir"; then
   test -f "$dir/result.json" || { echo "missing result: $dir" >&2; exit 1; }
  else
   rc=$?; printf '%s\n' "$rc" > "$dir/service-exit.code"
   echo "supplement failed, raw output retained: $unit" >&2
   exit "$rc"
  fi
  verify "$dir" "$batch" "$method"
 done
done < <(cat "$input/test.ids" "$input/dev.ids")
printf 'supplement complete %s\n' "$(date -Is)"
