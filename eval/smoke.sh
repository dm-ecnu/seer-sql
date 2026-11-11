#!/bin/bash
# Smoke run of every baseline on 20 evenly spaced batches per dataset (pipeline check, not the experiment matrix).
# On eval-server:  setsid nohup bash eval-wt/eval/smoke.sh OUTDIR [datasets...] > OUTDIR/smoke.log 2>&1 &
cd ${SEER_ROOT} && . ./env.sh
OUT=$1; shift
DS=${@:-spider_pairs bird_pairs minidev_pg_pairs leetcode_pairs spider_batches bird_batches minidev_pg_batches leetcode_batches cubes_d50 cubes_d100 tpch}
mkdir -p $OUT
for tool in sqlwitness parseval verieql polygon_pair polygon; do
  (for d in $DS; do
     venv-eval/bin/python eval-wt/eval/run_baseline.py --tool $tool --dataset datasets/batches/$d.jsonl \
       --sample 20 --budget 300 --per-call 60 --seed 0 --workers 6 --out $OUT/${tool}__$d.jsonl >> $OUT/${tool}.log 2>&1
   done) &
done
wait
