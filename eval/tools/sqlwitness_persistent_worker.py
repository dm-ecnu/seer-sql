"""JSON-lines transport around the unchanged released SQLWitness API."""
import json
import os
import sys
import time
import traceback

protocol = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

from sqlwitness_worker import handle

for line in sys.stdin:
    started = time.time()
    try:
        request = json.loads(line)
        if os.environ.get("SEER_ENTRY_METRICS"):
            from entry_metrics import install_native
            install_native(request)
        response = handle(request)
    except Exception as exc:
        response = {"verdict": "error", "reason": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "trace": traceback.format_exc()[-1500:]}
    response.setdefault("tool_secs", round(time.time() - started, 3))
    protocol.write(json.dumps(response, default=str) + "\n")
