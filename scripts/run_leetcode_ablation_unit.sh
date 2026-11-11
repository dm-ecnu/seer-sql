#!/bin/bash
set -euo pipefail
variant=${1:?variant}; bid=${2:?batch}; seed=${3:?seed}; output=${4:?output}
case "$variant" in full|no_deterministic_query_memo|no_hard_sigma) ;; *) exit 2;; esac
B=${SEER_ROOT}
W=$B/ablation-scale-wt
D=$B/datasets/batches/leetcode-scale-v1
. "$B/env2.sh"
export PYTHONHASHSEED="$seed" ARROW_DEFAULT_MEMORY_POOL=system MALLOC_ARENA_MAX=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
export PYTHONPATH="$W/src:$W/eval:$W/scripts"
ulimit -v 8388608
cd "$W"
mkdir "$output"
../venv-seer/bin/python scripts/hard_deadline.py --dataset "$D/leetcode_scale.jsonl" --only "$D/$bid.ids" --method pair_seer --ablation "$variant" --budget 60 --seed "$seed" --out "$output/raw.jsonl" --journal "$output/journal" > "$output/run.log" 2>&1
