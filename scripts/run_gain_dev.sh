#!/bin/bash
set -uo pipefail
cd ${SEER_ROOT}/resource-stop-wt
O=$PWD/runs/resource-stop-dev-v1
echo "start $(date -Is) $(uptime) cpus=$(cat "$O/cpu-set")"
xargs -r -n 1 -P 4 scripts/run_gain_dev_unit.sh < "$O/jobs.txt"
rc=$?
echo "end $(date -Is) $(uptime) exit=$rc"
exit "$rc"
