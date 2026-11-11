#!/usr/bin/env bash
set -euo pipefail
ablation=${1:?};batch=${2:?};seed=${3:?};output=${4:?}
root=${SEER_ROOT}
export TMPDIR=${TMPDIR}
export SEER_SQLWITNESS_SQLITE_SCRATCH="$TMPDIR"
export SQLITE_TMPDIR="$TMPDIR"
test -d "$TMPDIR"
tree="$root/stream-worker-progress-wt"
. "$root/env2.sh"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
export MALLOC_ARENA_MAX=2 PYTHONPATH="$tree/src:$tree/eval"
cd "$tree"
"$root/venv-seer/bin/python" scripts/run_stream_batch.py \
  --dataset "$root/datasets/batches/public-batch-v1/bird_public.jsonl" \
  --batch "$batch" --budget 600 --seed "$seed" --order shuffle \
  --method seer_stream --ablation "$ablation" --out "$output"
