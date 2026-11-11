#!/usr/bin/env bash
set -euo pipefail

seed=${1:?seed 0-4 required}
case "$seed" in 0|1|2|3|4) ;; *) exit 2;; esac
root=${SEER_ROOT}
export TMPDIR=${TMPDIR}
export SEER_SQLWITNESS_SQLITE_SCRATCH="$TMPDIR"
export SQLITE_TMPDIR="$TMPDIR"
test -d "$TMPDIR"
export XDG_RUNTIME_DIR=/run/user/$(id -u)
source_tree="$root/stream-tmp-mysql-wt"
input="$root/datasets/batches/leetcode-scale-v1"
output="$root/repro/stream-partition-v1/leetcode-ablation-matrix-v4"
expected_commit=$(git -C "$source_tree" rev-parse HEAD)
core=$((249 + seed))
ablations=(no_prover random_representative no_fingerprint_grouping no_pool_reuse)

mkdir -p "$output" "$output/service-logs"
exec 9>"$output/seed-$seed.lock"
flock -n 9
printf 'matrix start %s seed=%s core=%s uptime=%s\n' "$(date -Is)" "$seed" "$core" "$(uptime)"

verify_result() {
  "$root/venv-seer/bin/python" - "$1" "$expected_commit" "$2" "$3" "$seed" "$4" <<'PY'
import hashlib, json, pathlib, sys
p=pathlib.Path(sys.argv[1]); expected, batch, method, seed, ablation=sys.argv[2:]
c=json.loads((p/'config.json').read_text())
r=json.loads((p/'result.json').read_text()); m=r['metrics']
assert c['source_commit']==expected
assert (c['batch'], c['method'], c['seed'])==(batch, method, int(seed))
assert c['ablation']==ablation
assert c['method_seed']==0 and c['order']=='shuffle' and c['budget_s']==600
assert r['config_sha256']==hashlib.sha256((p/'config.json').read_bytes()).hexdigest()
assert m['invalid_admitted_certificates']==0
assert m['processed_queries']+m['unprocessed_queries']==m['input_queries']==len(c['query_ids'])
assert len(r['unprocessed_query_ids'])==m['unprocessed_queries']
assert sum(r['pair_counts'].values())==m['processed_queries']*(m['processed_queries']-1)//2
assert m['censored_at_budget'] == (not m['completed_within_budget'])
assert 'late_evidence_rejected' in m
assert 'late_arrivals_rejected' in m
assert all(e['wall_s'] < 600 for e in r['events']
           if e.get('event') in ('arrival','pool_added','evidence_edge_admitted'))
PY
}

while IFS= read -r batch; do
  test -n "$batch" || continue
  case "$batch" in *-n500) ;; *) continue ;; esac
  for offset in 0 1 2 3; do
    method=seer_stream
    ablation=${ablations[$(((seed + offset) % 4))]}
    dir="$output/$ablation/s$seed/$batch"
    if test -f "$dir/result.json"; then
      verify_result "$dir" "$batch" "$method" "$ablation"
      continue
    fi
    test ! -e "$dir" || { echo "incomplete output retained: $dir" >&2; exit 1; }
    mkdir -p "$(dirname "$dir")"
    unit="seer-lc-ablation-v4-${ablation}-s${seed}-${batch}"
    printf 'unit start %s %s uptime=%s\n' "$(date -Is)" "$unit" "$(uptime)"
    if systemd-run --user --wait --unit "$unit" \
      -p CPUAffinity="$core" -p CPUQuota=100% -p MemoryMax=10G \
      -p TasksMax=512 -p RuntimeMaxSec=900 \
      -p StandardOutput=append:"$output/service-logs/$unit.log" \
      -p StandardError=append:"$output/service-logs/$unit.log" \
      /bin/bash "$source_tree/scripts/run_leetcode_ablation_unit_v4.sh" \
      "$ablation" "$batch" "$seed" "$dir"; then
      test -f "$dir/result.json" || { echo "missing result: $dir" >&2; exit 1; }
    else
      rc=$?
      printf '%s\n' "$rc" > "$dir/service-exit.code"
      echo "unit failed, raw output retained: $unit" >&2
      exit "$rc"
    fi
    verify_result "$dir" "$batch" "$method" "$ablation"
    printf 'unit end %s %s uptime=%s\n' "$(date -Is)" "$unit" "$(uptime)"
  done
# The frozen scale source has 18 held-out parents and one development parent.
# Run all 19; the scorer keeps the development parent separate for inference.
done < <(cat "$input/test.ids" "$input/dev.ids")
printf 'matrix complete %s seed=%s uptime=%s\n' "$(date -Is)" "$seed" "$(uptime)"
