#!/usr/bin/env bash
set -euo pipefail
dataset=${1:?dataset}
batch=${2:?batch}
seed=${3:?seed}
output=${4:?output}
root=${SEER_ROOT}
tree="$root/sqlwitness-official-batch-wt"
export TMPDIR=${TMPDIR}
export SQLITE_TMPDIR="$TMPDIR"
export SEER_SQLWITNESS_SQLITE_SCRATCH="$TMPDIR"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
export MALLOC_ARENA_MAX=2
test -d "$TMPDIR"
source "$root/env2.sh"
export SEER_MYSQL_URL="${SEER_MYSQL_URL/23306/23308}"
export SQLWITNESS_MYSQL_URL="${SQLWITNESS_MYSQL_URL/23306/23308}"
export PYTHONPATH="$tree/src:$tree/eval"
cd "$tree"
exec "$root/venv-seer/bin/python" scripts/run_stream_batch.py \
  --dataset "$dataset" --batch "$batch" --budget 600 --seed "$seed" \
  --order shuffle --method sqlwitness --out "$output"
