#!/bin/bash
set -euo pipefail
B=${SEER_ROOT}
W=$B/public-batch-input-wt
D=$B/datasets/batches/public-batch-v1
O=$B/repro/public-bird-adapter-pilot-v1
test ! -e "$O"
mkdir "$O"
. "$B/env2.sh"
export ARROW_DEFAULT_MEMORY_POOL=system MALLOC_ARENA_MAX=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
ulimit -v 8388608
cd "$W"
echo "start $(date -Is) $(uptime)"
for method in pair_seer sqlwitness parseval; do
  echo "start method=$method $(date -Is)"
  if [ "$method" = pair_seer ]; then
    ../venv-seer/bin/python scripts/run_pair_seer.py --dataset "$D/bird_public.jsonl" --eval-root "$W" --seed 0 --budget 5 --shared-pair-replay --symbolic-selection representatives --mem-gb 8 --only "$D/pilot-dev.ids" --out "$O/$method.jsonl" > "$O/$method.log" 2>&1
  else
    ../venv-seer/bin/python eval/run_baseline.py --tool "$method" --comparison-state pairs --dataset "$D/bird_public.jsonl" --seed 0 --budget 5 --per-call 5 --restart --workers 1 --only "$D/pilot-dev.ids" --out "$O/$method.jsonl" > "$O/$method.log" 2>&1
  fi
  echo "end method=$method $(date -Is)"
done
../venv-seer/bin/python scripts/check_public_bird_pilot.py > "$O/checked.json"
echo "end $(date -Is) $(uptime)"
