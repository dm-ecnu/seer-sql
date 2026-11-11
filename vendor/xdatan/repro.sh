#!/bin/bash
# Reproduce XDataN's published results on its home benchmarks (XData-BM, TPCH).
# usage: repro.sh BM CORE   -> $XD_ROOT/repro/BM/{run.log,output/*.out}
set -u
XD=${XD_ROOT:-${SEER_ROOT}/xdatan}; BM=$1; CORE=$2
W=$XD/repro/$BM; rm -rf "$W"; mkdir -p "$W/privtmp"
{ echo "start $(date -Is) $(uptime)"
  LD_LIBRARY_PATH=$XD/src/xdata/z3 /usr/bin/time -v unshare -Urm sh -c "mount --bind '$W/privtmp' /tmp && exec taskset -c $CORE '$XD/venv/bin/python' -I '$XD/xd_run.py' '$XD/src/benchmarks/$BM' '$W'"
  echo "exit $? end $(date -Is) $(uptime)"; } > "$W/run.log" 2>&1
touch "$W/DONE"
