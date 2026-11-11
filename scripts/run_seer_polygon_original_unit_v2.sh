#!/usr/bin/env bash
set -euo pipefail

size=${1:?D-50 or D-100 size required}
batch=${2:?batch id required}
output=${3:?output directory required}
case "$size" in 50|100) ;; *) exit 2;; esac

root=${SEER_ROOT}
tree="$root/seer-m1m8-completeness-wt"
export TMPDIR=${TMPDIR}
export SQLITE_TMPDIR="$TMPDIR"
export SEER_SQLWITNESS_SQLITE_SCRATCH="$TMPDIR"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 MALLOC_ARENA_MAX=2
source "$root/env2.sh"
export SEER_MYSQL_URL="${SEER_MYSQL_URL/:23306/:23308}"
export SQLWITNESS_MYSQL_URL="${SQLWITNESS_MYSQL_URL/:23306/:23308}"
export SEER_EXPECTED_MYSQL_PORT=23308
case "$SEER_MYSQL_URL" in *:23308/*) ;; *) exit 2;; esac
export PYTHONPATH="$tree/src:$tree/eval"
cd "$tree"

exec "$root/venv-seer/bin/python" scripts/run_stream_batch.py \
    --dataset "$root/datasets/batches/cubes_d${size}.jsonl" \
    --batch "$batch" --budget 60 --seed 0 --order source \
    --method seer_stream --schedule tail_proof --out "$output"
