#!/bin/bash
set -euo pipefail
R=${SEER_ROOT}
export PIP_CACHE_DIR=$R/parseval25/pip-cache
export TMPDIR=$R/parseval25/tmp
export PIP_CONSTRAINT=$R/schema-adapter-wt/scripts/parseval_release007_py310.constraints.txt
mkdir -p "$PIP_CACHE_DIR" "$TMPDIR"
PY=$R/parseval25/venv007/bin/python
ulimit -v 8388608
echo "start $(date -Is) $(uptime)"
"$PY" -m pip install --disable-pip-version-check --progress-bar off hatchling editables
"$PY" -m pip install --disable-pip-version-check --progress-bar off --no-build-isolation -e "$R/parseval25/release007"
"$PY" -m pip freeze > "$R/repro/parseval/release007-environment.freeze"
"$PY" -c "import datafusion,parseval;print(\"native release imports passed\")"
echo "end $(date -Is) $(uptime)"
