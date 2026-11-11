#!/bin/bash
set -euo pipefail
. ${SEER_ROOT}/env2.sh
cd ${SEER_ROOT}/outcome-memo-wt
export PYTHONPATH=src:eval:. OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 SLURM_CPUS_ON_NODE=1
ulimit -v 8388608
D=runs/outcome-memo-kernel-v1
mkdir -p "$D"
exec 9>"$D/run.lock"
flock -n 9
trap 'printf "%s\n" "$?" > "$D/exit.code"' EXIT
timeout 7200 taskset -c 159 ../venv-seer/bin/python -m scripts.measure_outcome_memo
