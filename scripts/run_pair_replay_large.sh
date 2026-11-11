#!/bin/bash
set -euo pipefail
B=${SEER_ROOT}
. "$B/env2.sh"
cd "$B/pair-replay-shared-v2-wt"
export PYTHONPATH=src:eval:. OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 SLURM_CPUS_ON_NODE=1
ulimit -v 8388608
D=$B/pair-replay-shared-v2
mkdir -p "$D"
exec 9>"$D/run.lock"
flock -n 9
echo $$ > "$D/run.pid"
trap 'printf "%s\n" "$?" > "$D/exit.code"' EXIT
printf 'leetcode-1084\n' > "$D/large.ids"
echo "fixed 889-query public batch start $(date -Is) $(uptime)"
timeout 2100 taskset -c 159 ../venv-seer/bin/python scripts/run_pair_seer.py \
 --dataset "$B/datasets/batches/leetcode_batches.jsonl" --eval-root "$B/pair-replay-shared-v2-wt" \
 --seed 0 --budget 600 --symbolic-selection representatives --shared-pair-replay \
 --mem-gb 8 --only "$D/large.ids" --out "$D/leetcode-1084.s0.jsonl"
echo "fixed public batch execution ended $(date -Is) $(uptime)"
