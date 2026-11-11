#!/bin/bash
# Final scoring for the SEER main table (2026-10-22 protocol): wave 1 (SEER/v2 at d3c3696 dropped as superseded),
# its infrastructure-failure reruns, wave 2, and the SEER/v2 rerun at 9d5d194. Run in seer-mx2-wt on eval-server.
set -euo pipefail
W1=${SEER_ROOT}/seer-mx-wt/runs/server/mx
R=runs/server
OUT=$R/main_scores; mkdir -p $OUT
D=${SEER_ROOT}/datasets/batches
declare -A SAMPLE=([cubes_d50]=30 [cubes_d100]=15)
for ds in spider_pairs bird_pairs minidev_pg_pairs leetcode_batches cubes_d50 cubes_d100 tpch tpcds xdatabm tpch_mut; do
  extra=()
  [ -n "${SAMPLE[$ds]:-}" ] && extra+=(--sample "${SAMPLE[$ds]}")
  [ "$ds" = tpch ] && extra+=(--labels ${SEER_ROOT}/seer-mx-wt/runs/labels.jsonl)
  taskset -c "${CORE:-9}" ../venv-seer/bin/python scripts/matrix_score.py --dataset $D/$ds.jsonl \
    --runs $W1,$R/mx_infra,$R/mx2,$R/mx_seer --drop $W1:seer,v2 --split test --json $OUT/$ds.test.json "${extra[@]}" \
    > $OUT/$ds.test.txt
done
../venv-seer/bin/python scripts/main_table.py $OUT/*.test.json > $OUT/main_table.md
cat $OUT/main_table.md
