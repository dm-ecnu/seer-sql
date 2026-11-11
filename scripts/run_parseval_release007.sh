#!/bin/bash
set -euo pipefail
. ${SEER_ROOT}/env2.sh
export RAYON_NUM_THREADS=8 OPENBLAS_NUM_THREADS=1
cd ${SEER_ROOT}/schema-adapter-wt
ulimit -v 8388608
echo "start $(date -Is) $(uptime)"
../parseval25/venv007/bin/python scripts/run_parseval_release007.py --out ${SEER_ROOT}/repro/parseval/release007-native-full --workers 4
echo "end $(date -Is) $(uptime)"
