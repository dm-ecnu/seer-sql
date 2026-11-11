#!/usr/bin/env bash
set -euo pipefail
method=${1:?};batch=${2:?};seed=${3:?};output=${4:?}
root=${SEER_ROOT}
export TMPDIR=${TMPDIR}
export SEER_SQLWITNESS_SQLITE_SCRATCH="$TMPDIR"
export SQLITE_TMPDIR="$TMPDIR"
test -d "$TMPDIR"
tree="$root/stream-worker-progress-wt"
. "$root/env2.sh"
export SEER_MYSQL_URL="${SEER_MYSQL_URL/23306/23308}"
export SQLWITNESS_MYSQL_URL="${SQLWITNESS_MYSQL_URL/23306/23308}"
case "$SEER_MYSQL_URL:$SQLWITNESS_MYSQL_URL" in *:23308/*:23308/*) ;; *) echo "isolated MySQL port missing" >&2; exit 2;; esac
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
export MALLOC_ARENA_MAX=2 PYTHONPATH="$tree/src:$tree/eval"
cd "$tree"
"$root/venv-seer/bin/python" scripts/run_stream_batch.py \
  --dataset "$root/datasets/batches/cubes-full-v1/cubes_d100.jsonl" \
  --batch "$batch" --budget 600 --seed "$seed" --order shuffle \
  --method "$method" --out "$output"
