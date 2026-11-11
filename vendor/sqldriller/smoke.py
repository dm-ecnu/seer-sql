"""Smoke: SQLDriller worker on a few (reference, candidate) pairs, request built as tools.build_request would.
python smoke.py EVAL_DIR DATASET.jsonl N_BATCHES TIMEOUT  (run with venv-eval-like python, env.sh sourced)
env: SMOKE_DIFF=1 skips trivially equal pairs; SMOKE_SLACK = seconds allowed past TIMEOUT (default 60)"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import json, os, subprocess, sys, time

EVAL, DS, N, T = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
sys.path.insert(0, EVAL)
from seereval.schema import Schema, to_ddl  # noqa: E402
from seereval.engine import normalize_sql  # noqa: E402
from seereval import tools  # noqa: E402

SD = os.environ.get("SD_ROOT", (SEER_ROOT + "/sqldriller"))


def sqlsolver_ddl(s):
    simple = all(n.replace("_", "").isalnum() for t in s.tables for n in [t.name] + [c["name"] for c in t.columns])
    return ";\n".join(to_ddl(s, "mysql", deferrable=False, quote=not simple)) + ";"


root = os.path.dirname(os.path.abspath(DS))
batches = [json.loads(l) for l in open(DS)]
if os.environ.get("SMOKE_DIFF"):  # skip pairs that differ only in case, spacing or a trailing ';'
    flat = lambda q: " ".join(q.lower().rstrip().rstrip(";").split())
    batches = [b for b in batches if flat(b["queries"][0]["sql"]) != flat(b["queries"][min(2, len(b["queries"]) - 1)]["sql"])]
step = max(1, len(batches) // N)
for b in batches[::step][:N]:
    s = Schema.load(os.path.join(root, b["schema_path"]))
    qs = b["queries"]
    q1, q2 = qs[0]["sql"], qs[min(2, len(qs) - 1)]["sql"]
    req = tools.build_request("verieql", s, b["dialect"], [q1, q2], T, 0)
    if b["dialect"] == "sqlite":
        q1, q2 = (tools.sqlite_dq_literals(q, s) for q in (q1, q2))
    req.update(q1=normalize_sql(q1, b["dialect"]).strip().rstrip(";").strip(),
               q2=normalize_sql(q2, b["dialect"]).strip().rstrip(";").strip(),
               schema=s.to_json(), ddl=sqlsolver_ddl(s), seed=0)
    req.pop("bound", None)
    t = time.time()
    p = subprocess.run(["prlimit", f"--as={8 << 30}", "--", sys.executable, os.path.join(EVAL, "tools/sqldriller_worker.py")],
                       input=json.dumps(req, default=str), capture_output=True, text=True, timeout=T + float(os.environ.get("SMOKE_SLACK", 60)))
    try:
        r = json.loads(p.stdout)
    except ValueError:
        r = {"verdict": "worker_crash", "reason": p.stderr[-300:]}
    print(json.dumps({"batch": b["batch_id"], "verdict": r.get("verdict"), "stage": r.get("stage"),
                      "sqlsolver": r.get("sqlsolver"), "bounds": r.get("verieql_bounds"),
                      "ts_dbs": r.get("testsuite_dbs"), "stage_secs": r.get("stage_secs"),
                      "wall": round(time.time() - t, 1), "reason": (r.get("reason") or "")[:200]}), flush=True)
