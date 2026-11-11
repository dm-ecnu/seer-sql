#!/bin/bash
set -euo pipefail
B=${SEER_ROOT}
W=$B/pair-shared-dev-matrix-wt
O=$B/pair-shared-dev-matrix-v1
. "$B/env2.sh"
cd "$W"
export PYTHONPATH=src:eval:. ARROW_DEFAULT_MEMORY_POOL=system MALLOC_ARENA_MAX=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 SLURM_CPUS_ON_NODE=1
ulimit -v 8388608
exec 9>"$O/run.lock"
flock -n 9
echo $$ > "$O/run.pid"
../venv-seer/bin/python -m scripts.pair_shared_dev_protocol check
../venv-seer/bin/python - <<'PY'
import hashlib,json,time
from pathlib import Path
from scripts.pair_shared_dev_protocol import checked_config,BASE,ROOT
config,_=checked_config()
assert time.time()<config['original_deadline_unix'],'original deadline expired'
p=BASE/'pair-replay-shared-v2';assert (p/'exit.code').read_text().strip()=='0'
x=json.loads((p/'large-independent-score.json').read_text())
assert x['input_queries']==889 and x['pair_graph_edges']==394716
assert x['scored_reference_pairs']==888
assert x['source_sha256']==hashlib.sha256((p/'leetcode-1084.s0.jsonl').read_bytes()).hexdigest()
old=BASE/'pair-replay-shared-v2-wt'
paths=list((ROOT/'src').rglob('*.py'))+list((ROOT/'eval/seereval').glob('*.py'))
paths += [ROOT/'scripts/run_pair_seer.py',ROOT/'scripts/seer_sqlwitness.py']
for dest in paths:
 assert dest.read_bytes()==(old/dest.relative_to(ROOT)).read_bytes(),str(dest)
print('native completeness, independent replay, and algorithm identity accepted; no accuracy threshold',flush=True)
PY
for seed in 0 1 2 3 4; do
 for ds in tpcds leetcode_batches cubes_d50 cubes_d100; do
  name=$ds.pair_seer.s$seed
  stamp=$(../venv-seer/bin/python -m scripts.pair_shared_dev_protocol check)
  if [ -f "$O/$name.done" ]; then
   ../venv-seer/bin/python -m scripts.pair_shared_dev_protocol finish "$ds:pair_seer:$seed"
   continue
  fi
  echo "start $name $(date -Is) $(uptime) cpu=159 protocol=$stamp"
  if taskset -c 159 ../venv-seer/bin/python scripts/run_pair_seer.py --dataset "$B/datasets/batches/$ds.jsonl" --eval-root "$W" --seed "$seed" --budget 600 --symbolic-selection representatives --shared-pair-replay --mem-gb 8 --only "$B/gain-policy-wt/runs/gain-dev-v1/dev_ids/$ds.txt" --out "$O/$name.jsonl" > "$O/$name.log" 2>&1; then
   ../venv-seer/bin/python -m scripts.pair_shared_dev_protocol finish "$ds:pair_seer:$seed"
  else
   rc=$?; printf '%s\n' "$rc" > "$O/$name.failed"; exit "$rc"
  fi
  echo "end $name $(date -Is) $(uptime)"
 done
done
printf 'execution complete pair_seer shared replay %s\n' "$(date -Is)"
