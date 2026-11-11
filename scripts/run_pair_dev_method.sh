#!/bin/bash
set -euo pipefail
method=${1:?method required}
case "$method" in pair_seer) core=160;; sqlwitness) core=161;; parseval) core=162;; xdatan) core=163;; *) exit 2;; esac
B=${SEER_ROOT}
W=$B/pair-dev-matrix-wt
O=$B/pair-dev-matrix-v1
. "$B/env2.sh"
cd "$W"
export ARROW_DEFAULT_MEMORY_POOL=system MALLOC_ARENA_MAX=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
ulimit -v 8388608
exec 9>"$O/$method.lock"
flock -n 9
echo $$ > "$O/$method.pid"
for seed in 0 1 2 3 4; do
 for ds in tpcds leetcode_batches cubes_d50 cubes_d100; do
  name=$ds.$method.s$seed
  stamp=$(../venv-seer/bin/python -m scripts.pair_dev_protocol check)
  if [ -f "$O/$name.done" ]; then
   ../venv-seer/bin/python -m scripts.pair_dev_protocol finish "$ds:$method:$seed"
   continue
  fi
  echo "start $name $(date -Is) $(uptime) cpu=$core protocol=$stamp"
  if [ "$method" = pair_seer ]; then
   args=(../venv-seer/bin/python scripts/run_pair_seer.py --dataset "$B/datasets/batches/$ds.jsonl" --eval-root "$W" --seed "$seed" --budget 600 --symbolic-selection representatives --mem-gb 8 --only "$B/gain-policy-wt/runs/gain-dev-v1/dev_ids/$ds.txt" --out "$O/$name.jsonl")
  else
   args=(../venv-seer/bin/python eval/run_baseline.py --tool "$method" --comparison-state pairs --dataset "$B/datasets/batches/$ds.jsonl" --seed "$seed" --budget 600 --per-call 60 --restart --workers 1 --only "$B/gain-policy-wt/runs/gain-dev-v1/dev_ids/$ds.txt" --out "$O/$name.jsonl")
  fi
  if taskset -c "$core" "${args[@]}" > "$O/$name.log" 2>&1; then
   ../venv-seer/bin/python -m scripts.pair_dev_protocol finish "$ds:$method:$seed"
  else
   rc=$?; printf '%s\n' "$rc" > "$O/$name.failed"; exit "$rc"
  fi
  echo "end $name $(date -Is) $(uptime)"
 done
done
printf 'execution complete %s %s\n' "$method" "$(date -Is)"
