#!/bin/bash
set -eu
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
B=${ICDE_ROOT}
W=$B/watch
T=$(mktemp "$W/hourly.XXXXXX")
finish() {
  rc=$?
  trap - EXIT
  echo "Hourly checker exit_status=$rc" >> "$T"
  cat "$T" >> "$W/hourly.log"
  mv "$T" "$W/hourly.txt"
  exit "$rc"
}
trap finish EXIT
{
  date --iso-8601=seconds
  cat "$W/status.txt"
  if [ -f "$B/seer/repro/DONE" ]; then
    echo 'SEER reproduction gate: OPEN; verify certification before paper claims'
  else
    echo 'SEER reproduction gate: CLOSED; timing schedulers waiting is expected'
  fi
  echo 'Gateway network health:'
  curl -sS --max-time 5 -o /dev/null -w 'gateway_http=%{http_code}\n' http://127.0.0.1:4000/health/liveliness || true
  echo 'Original Spider run is transport-affected; use the separate recovery certification below.'
  if [ -f "$B/seer/repro/official/spider-recovered-s0.run.log" ]; then
    tail -n 3 "$B/seer/repro/official/spider-recovered-s0.run.log"
    systemctl --user show seer-official-spider-recovery.scope -p ActiveState -p TasksCurrent || echo "recovery scope query failed; inspect process state"
  fi
  echo "Approved sampling-priority gate check:"
  timeout 300s taskset -c 18 ${ECHO_WORK}/venv/bin/python "$B/echo/sys/scripts/release_sampling_gates.py" --apply || echo "priority gate check incomplete; no new readiness claim"
  echo "ParSEval isolated release setup (not a changed main-table version):"
  systemctl --user show seer-parseval007-locked-setup.service -p ActiveState -p SubState -p ExecMainStatus || true
  if [ -f "$B/seer/repro/parseval/release007-locked-setup.log" ]; then
    tail -n 5 "$B/seer/repro/parseval/release007-locked-setup.log"
  fi
  echo 'SQLDriller official runner:'
  pgrep -af 'repro_sqldriller_official.py|repro/official/run.sh' || true
  tail -n 15 "$B/seer/repro/official/run.log"
  echo "ECHO sound-certificate replay (fixed inputs; no new LLM calls):"
  systemctl --user show echo-certificate-analysis.service echo-certificate-matrix.service -p Id -p ActiveState -p SubState -p ExecMainStatus -p TasksCurrent || true
  if [ -f "$B/echo/logs/certificate-matrix.log" ]; then tail -n 6 "$B/echo/logs/certificate-matrix.log"; fi
  find "$B/echo/certificate-wt/runs/certificate-queue" -maxdepth 1 -name "*.failed" -printf "%f\n" 2>/dev/null || true
  echo "ParSEval unchanged release native full run:"
  systemctl --user show seer-parseval007-native-full.service -p ActiveState -p SubState -p ExecMainStatus -p TasksCurrent || true
  if [ -f "$B/seer/repro/parseval/release007-native-full.log" ]; then tail -n 4 "$B/seer/repro/parseval/release007-native-full.log"; fi
  if [ -f "$B/seer/repro/parseval/release007-native-full/completed.jsonl" ]; then wc -l "$B/seer/repro/parseval/release007-native-full/completed.jsonl"; fi
  echo 'Common-validator certificate check:'
  for b in bird spider; do
    if [ -s "$B/seer/repro/official/$b.jsonl" ]; then
      timeout 180s taskset -c 17 "$B/seer/venv-seer/bin/python" "$B/seer/repro/official/check_certificates.py" "$b" || echo "$b certificate check incomplete or failed; inspect logs"
      timeout 180s taskset -c 17 "$B/seer/venv-seer/bin/python" "$B/seer/repro/official/check_certificates.py" "$b" --schema-source native-fixed || echo "$b native-schema check incomplete or failed; inspect logs"
    fi
  done
  if [ -s "$B/seer/repro/official/spider-recovered-s0.jsonl" ]; then
    timeout 180s taskset -c 17 "$B/seer/venv-seer/bin/python" "$B/seer/repro/official/check_certificates.py" spider --run-stem spider-recovered-s0 --schema-source native-fixed || echo 'recovery certificate check failed'
  fi
  echo 'Official resources and results:'
  find "$B/seer/sqldriller/resources" -maxdepth 1 -type f -printf '%f bytes=%s modified=%TY-%Tm-%TdT%TH:%TM\n'
  find "$B/seer/repro/official" -maxdepth 1 -name '*.jsonl' -exec wc -l {} +
  echo 'Changes within the past hour (mtime, byte size, path):'
  find "$B/echo/sys/runs/eval" "$B/seer/repro/official" -type f -mmin -60 -printf '%T+ %s %p\n' | sort | tail -n 30
  echo 'Scope: evidence checks plus release of existing approved priority gates after all x5 source pools validate; no paper edits, method conclusions, service restarts or protocol changes.'
} > "$T" 2>&1
