#!/bin/bash
set -euo pipefail
R=${SEER_ROOT}
W=$R/resource-stop-wt
O=$W/runs/resource-stop-dev-v1
IFS=: read -r dataset seed policy <<< "$1"
case "$dataset" in tpcds|leetcode_batches|cubes_d50|cubes_d100) ;; *) exit 2;; esac
case "$seed" in 0|1|2|3|4) ;; *) exit 2;; esac
case "$policy" in representatives|max_pairs) ;; *) exit 2;; esac
cd "$W"
. "$R/env2.sh"
export ARROW_DEFAULT_MEMORY_POOL=system MALLOC_ARENA_MAX=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
name=$dataset.s$seed.$policy
on_exit() {
  rc=$?
  if [ "$rc" -ne 0 ]; then printf "%s\n" "$rc" > "$O/$name.failed"; fi
}
trap on_exit EXIT
git diff --quiet HEAD -- src scripts eval
exec 9>"$O/$name.lock"
flock 9
stamp=$("$R/venv-seer/bin/python" - "$dataset" "$seed" "$policy" <<"PY"
import hashlib,subprocess,sys
from pathlib import Path
r=Path("${SEER_ROOT}");w=r/"resource-stop-wt";ds,seed,pol=sys.argv[1:]
h=hashlib.sha256((seed+pol+"budget600_as8G").encode())
h.update(subprocess.check_output(["git","rev-parse","HEAD"]))
for p in [r/("datasets/batches/"+ds+".jsonl"),w/("runs/resource-stop-dev-v1/dev_ids/"+ds+".txt")]:h.update(p.read_bytes())
print(h.hexdigest())
PY
)
if [ -f "$O/$name.done" ]; then test "$(cat "$O/$name.done")" = "$stamp"; exit 0; fi
if [ -f "$O/$name.started" ]; then test "$(cat "$O/$name.started")" = "$stamp"; fi
printf "%s\n" "$stamp" > "$O/$name.started"
printf "start %s %s\n" "$name" "$(date -Is)"
set +e
taskset -c "$(cat "$O/cpu-set")" "$R/venv-seer/bin/python" scripts/seer_sqlwitness.py --eval-root . --dataset "$R/datasets/batches/$dataset.jsonl" --method seer --budget 600 --seed "$seed" --symbolic-selection "$policy" --mem-gb 8 --only "$O/dev_ids/$dataset.txt" --out "$O/$name.jsonl" > "$O/$name.log" 2>&1
rc=$?
set -e
if [ "$rc" -ne 0 ]; then printf "%s\n" "$rc" > "$O/$name.failed"; exit "$rc"; fi
"$R/venv-seer/bin/python" - "$O" "$name" "$dataset" "$seed" "$policy" <<"PY"
import json,sys
from pathlib import Path
o,name,ds,seed,pol=sys.argv[1:];o=Path(o)
want=set((o/"dev_ids"/(ds+".txt")).read_text().splitlines())
rows=[json.loads(l) for l in (o/(name+".jsonl")).read_text().splitlines()]
assert len(rows)==len(want) and {r["batch_id"] for r in rows}==want
assert all(r["seed"]==int(seed) and r["budget"]==600 and r["provenance"]["symbolic_selection"]==pol for r in rows)
print(json.dumps({"unit":name,"records":len(rows),"runner_errors":sum(bool(r.get("runner_error")) for r in rows),"scope":"execution_complete_including_errors; not_a_main_table_gate"}),flush=True)
PY
printf "%s\n" "$stamp" > "$O/$name.done"
printf "end %s %s\n" "$name" "$(date -Is)"
