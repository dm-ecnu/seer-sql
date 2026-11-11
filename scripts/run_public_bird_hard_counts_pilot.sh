#!/bin/bash
set -euo pipefail
B=${SEER_ROOT}
W=$B/hard-deadline-counts-wt
D=$B/datasets/batches/public-batch-v1
O=$B/repro/public-bird-hard-deadline-counts-pilot-v1
test ! -e "$O"
mkdir "$O"
. "$B/env2.sh"
export ARROW_DEFAULT_MEMORY_POOL=system MALLOC_ARENA_MAX=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
ulimit -v 8388608
cd "$W"
echo "start $(date -Is) $(uptime)"
for method in pair_seer sqlwitness parseval xdatan; do
  echo "start method=$method $(date -Is)"
  ../venv-seer/bin/python scripts/hard_deadline.py --dataset "$D/bird_public.jsonl" --only "$D/pilot-dev.ids" --method "$method" --budget 20 --seed 0 --out "$O/$method.jsonl" --journal "$O/$method-journal" > "$O/$method.log" 2>&1
  echo "end method=$method $(date -Is)"
done
../venv-seer/bin/python scripts/check_hard_deadline_counts_pilot.py > "$O/checked.json"
echo "end $(date -Is) $(uptime)"
