#!/usr/bin/env bash
# Run SEER on every group of a batch file, one process per group pinned to its own core (taskset), each with an
# address-space cap (--mem-gb), P groups at a time. Records uptime at start and end.
# usage: scripts/seer_run_groups.sh BATCH.jsonl SCHEMA.sql OUTDIR FIRST_CORE P [extra seer batch args...]
set -euo pipefail
batch=$1; schema=$2; out=$3; core0=$4; par=$5; shift 5
mkdir -p "$out"
py=${PY:-python}
uptime > "$out/uptime_start.txt"
groups=$($py -c "import json,sys; seen=[]; [seen.append(str(json.loads(l).get('batch','all'))) for l in open(sys.argv[1]) if l.strip() and str(json.loads(l).get('batch','all')) not in seen]; print(' '.join(seen))" "$batch")
i=0
for g in $groups; do
  echo "$i $g"
  i=$((i+1))
done | xargs -P "$par" -n 2 sh -c '
  core=$(( '"$core0"' + $0 % '"$par"' ))
  PYTHONPATH=src taskset -c $core '"$py"' -m seer batch --schema '"$schema"' --batch '"$batch"' --groups "$1" \
     --out '"$out"'/g_"$1".jsonl '"$*"' > '"$out"'/g_"$1".log 2>&1 || echo "group $1 exit $?" >> '"$out"'/failures.txt
'
uptime > "$out/uptime_end.txt"
cat "$out"/g_*.jsonl > "$out/all.jsonl"
