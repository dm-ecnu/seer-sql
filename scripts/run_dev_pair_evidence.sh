#!/bin/bash
set -euo pipefail
. ${SEER_ROOT}/env2.sh
cd ${SEER_ROOT}/pair-state-wt
export ARROW_DEFAULT_MEMORY_POOL=system MALLOC_ARENA_MAX=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
ulimit -v 8388608
printf "start %s\n" "$(date -Is)"
uptime
../venv-seer/bin/python scripts/replay_dev_pair_evidence.py
printf "end %s\n" "$(date -Is)"
uptime
