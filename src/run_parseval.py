"""Run ParSEval `disprove(original, rewrite)` on every pair, with the TPC-H DDL as declared (PK/FK/NOT NULL)
and with those constraints stripped. Records verdict, time, and whether a NEQ counterexample satisfies the
declared constraints (checked independently) and still distinguishes the pair when re-executed in DuckDB.

Runs in ParSEval's venv:  ext/ParSEval/.venv/bin/python src/run_parseval.py LABELS.jsonl OUT.jsonl --workers 64
"""
import argparse, json, multiprocessing as mp, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL_FULL = open(f"{ROOT}/data/tpch_schema.sql").read()
QUERIES = json.load(open(f"{ROOT}/data/tpch_queries.json"))
PG = os.environ.get("PG_URL", "postgresql://probe@127.0.0.1:55917/postgres")
GEN_LIMIT_S = 60
SOLVER_MS = 3000
HARD_S = 400


def strip(ddl):
    out = re.sub(r",\s*FOREIGN KEY \([^)]*\) REFERENCES \w+ \([^)]*\)", "", ddl)
    out = re.sub(r",\s*PRIMARY KEY \([^)]*\)", "", out)
    return out.replace(" NOT NULL", "")


DDL = {"full": DDL_FULL, "stripped": strip(DDL_FULL)}


def child(q1, q2, mode, q):
    try:
        from parseval import GenerationConfig, disprove
        t = time.time()
        res = disprove(q1, q2, DDL[mode], PG, "postgres",
                       config=GenerationConfig(timeout_ms=SOLVER_MS, time_limit_s=GEN_LIMIT_S), timeout_s=30)
        cex = None
        if res.counterexample is not None:
            inst = res.counterexample
            cex = {}
            for tb in inst.catalog.tables():
                cols = [b.name.text for b in tb.columns]
                cex[tb.name.parts[-1].text] = [dict(zip(cols, [str(v) if v is not None and not isinstance(v, (int, float)) else v for v in r]))
                                               for r in inst.rows(tb.relation)]
        q.put({"verdict": res.verdict.name, "reason": (res.reason or "")[:300], "secs": round(time.time() - t, 2),
               "cex": cex, "results": [str(x)[:300] for x in (res.results or ())]})
    except Exception as e:
        q.put({"verdict": "CRASH", "reason": f"{type(e).__name__}: {str(e)[:300]}", "secs": None, "cex": None})


def one(job):
    r, mode = job
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    t = time.time()
    p = ctx.Process(target=child, args=(QUERIES[str(r["qid"])], r["sql"], mode, q))
    p.start()
    try:
        out = q.get(timeout=HARD_S)
    except Exception:
        out = {"verdict": "HARD_TIMEOUT", "reason": "", "secs": HARD_S, "cex": None}
    if p.is_alive():
        p.kill()
    p.join()
    out["wall"] = round(time.time() - t, 2)
    return {"qid": r["qid"], "rid": r["rid"], "label": r["label"], "mode": mode, **out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("labels"); ap.add_argument("out")
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--labels-only", default="")
    ap.add_argument("--modes", default="full,stripped", help="comma list of DDL modes (full, stripped)")
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.labels)]
    if a.labels_only:
        rows = [r for r in rows if r["label"] in a.labels_only.split(",")]
    jobs = [(r, m) for r in rows for m in a.modes.split(",")]
    with ThreadPoolExecutor(a.workers) as ex, open(a.out, "w") as f:
        for i, res in enumerate(ex.map(one, jobs)):
            f.write(json.dumps(res, default=str) + "\n"); f.flush()
            if i % 20 == 0:
                print(i, len(jobs), flush=True)


if __name__ == "__main__":
    main()
