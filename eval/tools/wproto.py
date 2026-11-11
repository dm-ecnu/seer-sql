"""Worker protocol shared by the tool adapters. Each adapter runs in its tool's own venv as a child process:
one JSON request on stdin, one JSON response on the original stdout. The tools print freely, so stdout is
redirected to stderr for the duration of the call and the response goes to the saved descriptor."""
import json, os, sys, time, traceback


def serve(handler):
    req = json.loads(sys.stdin.read())
    out_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    t = time.time()
    try:
        if os.environ.get("SEER_ENTRY_METRICS"):
            from entry_metrics import install_native
            install_native(req)
        resp = handler(req)
    except Exception as e:  # recorded, never raised past the worker
        unsup = isinstance(e, (NotImplementedError, SyntaxError)) or any(
            k in type(e).__name__ for k in ("Parse", "Syntax", "Unsupported", "NotSupported", "Lark", "Unexpected"))
        resp = {"verdict": "unsupported" if unsup else "error", "reason": f"{type(e).__name__}: {str(e)[:300]}",
                "trace": traceback.format_exc()[-1500:]}
    resp.setdefault("tool_secs", round(time.time() - t, 3))
    with os.fdopen(out_fd, "w") as f:
        f.write(json.dumps(resp, default=str))
        f.flush()


def header_rows_to_instance(cex):
    """{table: [header, row, ...]} -> {table: [{col: val}, ...]}"""
    return {t: [dict(zip(rows[0], r)) for r in rows[1:]] for t, rows in (cex or {}).items() if rows}
