#!/bin/bash
set -euo pipefail
method=${1:?method}; bid=${2:?batch id}; seed=${3:?seed}; output=${4:?output dir}
case "$method" in pair_seer|sqlwitness|parseval|xdatan) ;; *) exit 2;; esac
case "$bid" in leetcode-*-n*) ;; *) exit 2;; esac
B=${SEER_ROOT}
W=$B/duckdb-worker-wt
D=$B/datasets/batches/leetcode-scale-v1
. "$B/env2.sh"
export ARROW_DEFAULT_MEMORY_POOL=system MALLOC_ARENA_MAX=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
ulimit -v 8388608
cd "$W"
mkdir "$output"
echo "start $(date -Is) $(uptime) method=$method batch=$bid seed=$seed"
../venv-seer/bin/python scripts/hard_deadline.py --dataset "$D/leetcode_scale.jsonl" --only "$D/$bid.ids" --method "$method" --budget 60 --seed "$seed" --out "$output/raw.jsonl" --journal "$output/journal" > "$output/run.log" 2>&1
echo "end $(date -Is) $(uptime)"
