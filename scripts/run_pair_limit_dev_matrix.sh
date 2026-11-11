#!/bin/bash
set -euo pipefail
B=${SEER_ROOT}
W=$B/nondet-limit-wt
O=$B/pair-limit-dev-matrix-v1
. "$B/env2.sh"
cd "$W"
export PYTHONPATH=src:eval:. ARROW_DEFAULT_MEMORY_POOL=system MALLOC_ARENA_MAX=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 SLURM_CPUS_ON_NODE=1
ulimit -v 8388608
exec 9>"$O/run.lock"
flock -n 9
echo $$ > "$O/run.pid"
../venv-seer/bin/python -m scripts.pair_limit_dev_protocol check
../venv-seer/bin/python - <<'PY'
import time
from scripts.pair_limit_dev_protocol import checked_config
config,_=checked_config()
assert time.time()<config['original_deadline_unix'],'original deadline expired'
assert config['algorithm_revision']=='1bea5fec520f7236fc9752c24c40385865574fb3'
assert config['legal_tie_completions'] and config['proof_scope_guard']
print('complete-system regression, original public inputs and fixed corrected algorithm accepted; no accuracy threshold',flush=True)
PY
for seed in 0 1 2 3 4; do
 for ds in tpcds leetcode_batches cubes_d50 cubes_d100; do
  name=$ds.pair_seer.s$seed
  stamp=$(../venv-seer/bin/python -m scripts.pair_limit_dev_protocol check)
  if [ -f "$O/$name.done" ]; then
   ../venv-seer/bin/python -m scripts.pair_limit_dev_protocol finish "$ds:pair_seer:$seed"
   continue
  fi
  if [ -e "$O/$name.jsonl" ] || [ -e "$O/$name.log" ] || [ -e "$O/$name.failed" ]; then
   echo "incomplete or failed attempt retained; refusing automatic retry: $name" >&2; exit 1
  fi
  echo "start $name $(date -Is) $(uptime) cpu=157 protocol=$stamp"
  if taskset -c 157 ../venv-seer/bin/python scripts/run_pair_seer.py --dataset "$B/datasets/batches/$ds.jsonl" --eval-root "$W" --seed "$seed" --budget 600 --symbolic-selection representatives --shared-pair-replay --mem-gb 8 --only "$B/gain-policy-wt/runs/gain-dev-v1/dev_ids/$ds.txt" --out "$O/$name.jsonl" > "$O/$name.log" 2>&1; then
   ../venv-seer/bin/python -m scripts.pair_limit_dev_protocol finish "$ds:pair_seer:$seed"
  else
   rc=$?; printf '%s\n' "$rc" > "$O/$name.failed"; exit "$rc"
  fi
  echo "end $name $(date -Is) $(uptime)"
 done
done
../venv-seer/bin/python -m scripts.pair_limit_dev_protocol check
printf 'execution complete corrected pair_seer shared replay %s\n' "$(date -Is)"
