#!/usr/bin/env bash
set -euo pipefail
ablation=${1:?}; batch=${2:?}; seed=${3:?}; output=${4:?}
root=${SEER_ROOT}
tree="$root/stream-arrival-wt"
source_data="$root/datasets/batches/leetcode-scale-v1/leetcode_scale.jsonl"
. "$root/env2.sh"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
export MALLOC_ARENA_MAX=2 PYTHONPATH="$tree/src:$tree/eval"
cd "$tree"
"$root/venv-seer/bin/python" scripts/run_stream_batch.py \
  --dataset "$source_data" --batch "$batch" --budget 600 \
  --seed "$seed" --order shuffle --method seer_stream --ablation "$ablation" --out "$output"
