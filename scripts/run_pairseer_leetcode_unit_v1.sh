#!/usr/bin/env bash
set -euo pipefail
arrival_seed=${1:?arrival seed 0-4}
batch=${2:?held-out n500 batch}
output=${3:?new output directory}
case "$arrival_seed" in 0|1|2|3|4) ;; *) exit 2;; esac
case "$batch" in leetcode-*-n500) ;; *) exit 2;; esac

base=${SEER_ROOT}
tree="$base/stream-worker-progress-wt"
input="$base/repro/stream-partition-v1/pairseer-leetcode-main-v1/input"
source="$input/arrivals-s$arrival_seed.jsonl"
only="$base/datasets/batches/leetcode-scale-v1/$batch.ids"
expected_commit=d4ff8d2a4d4d83c8f4dd6cd6a72f686605d2b4a7
auxiliary_commit=$(git -C "$base/stream-scale-score-v10-wt" rev-parse HEAD)
test "$(git -C "$tree" rev-parse HEAD)" = "$expected_commit"
test -f "$source" && test -f "$only" && test ! -e "$output"
test "$(cat "$only")" = "$batch"
mkdir "$output"

. "$base/env2.sh"
export TMPDIR=${TMPDIR}
export SQLITE_TMPDIR="$TMPDIR" SEER_SQLWITNESS_SQLITE_SCRATCH="$TMPDIR"
export SEER_MYSQL_URL="${SEER_MYSQL_URL/23306/23308}"
export SQLWITNESS_MYSQL_URL="${SQLWITNESS_MYSQL_URL/23306/23308}"
export PYTHONHASHSEED=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
export MALLOC_ARENA_MAX=2 ARROW_DEFAULT_MEMORY_POOL=system
export PYTHONPATH="$tree/src:$tree/eval:$tree/scripts"
test -d "$TMPDIR"
case "$SEER_MYSQL_URL:$SQLWITNESS_MYSQL_URL" in *:23308/*:23308/*) ;; *) exit 2;; esac
ulimit -v 8388608

"$base/venv-seer/bin/python" - "$source" "$only" "$output" "$arrival_seed" "$expected_commit" "$auxiliary_commit" <<'PY'
import hashlib,json,os,pathlib,sys
source,only,out,seed,commit,auxiliary=sys.argv[1:]
hash_file=lambda p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
config={'method':'pair_seer','batch':pathlib.Path(only).stem,'arrival_seed':int(seed),
        'method_seed':0,'budget_s':600,'per_call_s':60,'goal_policy':'all_pairs',
        'ablation':'full','source_commit':commit,'dataset':source,
        'auxiliary_commit':auxiliary,
        'dataset_sha256':hash_file(source),'only':only,'only_sha256':hash_file(only),
        'temporary_directory':os.environ['TMPDIR'],'affinity':sorted(os.sched_getaffinity(0))}
pathlib.Path(out,'config.json').write_text(json.dumps(config,sort_keys=True,indent=2)+'\n')
PY
cd "$tree"
exec "$base/venv-seer/bin/python" scripts/hard_deadline.py \
  --dataset "$source" --only "$only" --method pair_seer \
  --ablation full --goal-policy all_pairs --budget 600 --seed 0 \
  --out "$output/raw.jsonl" --journal "$output/journal"
