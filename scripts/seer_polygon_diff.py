"""Differential test: SEER's ported encoding vs. the vendored, unmodified Polygon (vendor/polygon/sql_testing).

Both tools get the same constraint set Σ_P, i.e. what Polygon's input language expresses: primary keys (Polygon
'primary' = distinct + not null), single-column foreign keys (Polygon adds {'eq': [child, parent]} from FKeys),
NOT NULL. CHECK, UNIQUE and composite foreign keys are dropped for BOTH tools (SEER sees the re-emitted DDL).
Same bound: Polygon bound=k with its author default_k=16 per operator (evaluation/calcite/online.py); SEER runs
only the symbolic tier at level k (no fast tier, no deepening). Same wall budget per pair.
Value domains differ by construction (Polygon: untyped Int cells, hashed strings; SEER: typed terms) — that is a
listed fix in PATCHES.md, so disagreements caused by it are expected to land in the "Polygon bug" column.

Adjudication of every decided disagreement, on the real engine (DuckDB) under Σ_P:
  Polygon NEQ / SEER EQ  -> Polygon's counterexample is decoded, checked against Σ_P (Schema.violations) and both
                            queries are executed on it; a Σ_P-valid instance that separates them means a SEER bug,
                            otherwise a Polygon bug (spurious counterexample).
  Polygon EQ  / SEER NEQ -> SEER's certificate is replayed on a fresh DuckDB connection and checked against Σ_P
                            and the bound; a reproduced, valid, in-bound witness means a Polygon bug (missed
                            counterexample within its own bound), otherwise a SEER bug.
Pairs one side does not decide (Polygon ERR/TIMEOUT, SEER unsupported/undecided) are counted, not adjudicated.

Usage (server; two venvs because Polygon pins its own dependencies):
  python scripts/seer_polygon_diff.py --suite all --out runs/server/polydiff/diff.jsonl --workers 11 \
      --polygon-py $SEER_ROOT/venv-pg/bin/python --budget 120 --k 2 --mem-gb 8
  python scripts/seer_polygon_diff.py --summarize runs/server/polydiff/diff.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import resource
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLY_PATH = os.path.join(ROOT, "vendor", "polygon")
OPS = ("filter", "project", "union", "inner join", "left join", "right join", "full join", "product", "order by")


def _cap(mem_gb):
    os.environ.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", MALLOC_ARENA_MAX="2")
    if mem_gb and sys.platform.startswith("linux"):
        b = int(mem_gb * 2**30)
        resource.setrlimit(resource.RLIMIT_AS, (b, b))


# ------------------------------------------------------------------ Σ_P: the constraint set both tools receive
def restricted_schema(ddl):
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from seer2.schema import Schema
    S = Schema(ddl)
    for t in S.tables.values():
        t.checks, t.uniques = [], []
        t.fks = [fk for fk in t.fks if len(fk.cols) == 1]
    return Schema(";\n".join(S.ddl_for("duckdb")) + ";")


_PT = {"int": "int", "dec": "numeric", "float": "numeric", "str": "varchar", "date": "date", "bool": "bool",
       "ts": "int"}


def _ptype(c):
    return _PT[c.type]


def polygon_input(S):
    order = list(S.order)
    idx = {t: i for i, t in enumerate(order)}
    tables = []
    for t in order:
        tb = S.tables[t]
        pk = list(tb.pk or [])
        tables.append({"TableName": t,
                       "PKeys": [{"Name": c, "Type": _ptype(tb.col(c))} for c in pk],
                       "FKeys": [{"FName": fk.cols[0], "PName": fk.ref_cols[0], "PTable": str(idx[fk.ref])}
                                 for fk in tb.fks],
                       "Others": [{"Name": c.name, "Type": _ptype(c)} for c in tb.columns if c.name not in pk]})
    cons = [{"not_null": f"{t}.{c.name}"} for t in order for c in S.tables[t].columns
            if not c.nullable and c.name not in (S.tables[t].pk or [])]
    return tables, cons


# ------------------------------------------------------------------ pair sources
def pairs(suite):
    out = []
    if suite in ("tpch", "all"):
        ddl = open(os.path.join(ROOT, "data", "tpch_schema.sql")).read()
        Q = json.load(open(os.path.join(ROOT, "data", "tpch_queries.json")))
        for l in open(os.path.join(ROOT, "runs", "labels.jsonl")):
            r = json.loads(l)
            if r["rid"] == 0 or r["label"] == "error":
                continue
            out.append({"id": f"tpch:{r['qid']}.{r['rid']}", "schema": "tpch", "label": r["label"],
                        "q1": Q[str(r["qid"])] if str(r["qid"]) in Q else Q[r["qid"]], "q2": r["sql"], "ddl": ddl})
    if suite in ("cases", "all"):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import seer_cases
        for name, a, b, exp in seer_cases.CASES:
            out.append({"id": f"case:{name}", "schema": "cases", "label": exp, "q1": a, "q2": b,
                        "ddl": seer_cases.DDL})
    return out


# ------------------------------------------------------------------ one side, in a child process
def run_polygon(job, k, budget):
    import logging
    sys.path.insert(0, POLY_PATH)
    from sql_testing.environment import Environment
    from sql_testing.logger import logger
    logger.setLevel(logging.ERROR)
    tables, cons = job["poly"]
    t0 = time.time()
    env = Environment(tables, cons, bound=k, default_k={o: 16 for o in OPS}, time_budget=int(budget))
    try:
        eq, cex, _, _, ret = env.check(job["q1"], job["q2"], use_precise_encoding=False)
    except Exception as e:
        return {"v": "ERR", "reason": f"{type(e).__name__}: {str(e)[:160]}", "secs": round(time.time() - t0, 2)}
    status = ret.get("status") if isinstance(ret, dict) else None
    v = {True: "EQ", False: "NEQ"}.get(eq, "TIMEOUT" if status == "TMO" else "ERR")
    out = {"v": v, "status": status, "secs": round(time.time() - t0, 2)}
    if cex:
        out["cex"] = {t: [dict(zip(rows[0], r)) for r in rows[1:]] for t, rows in cex.items() if rows}
    return out


def run_seer(job, k, budget):
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from seer.core import judge_pair
    S = restricted_schema(job["ddl"])
    t0 = time.time()
    r = judge_pair(S, job["q1"], job["q2"], dialect="duckdb", backend="duckdb", budget=budget, levels=(k,),
                   fast=False, symbolic=True)
    p = r["pair"]
    v = {"split": "NEQ", "bounded_equivalent": "EQ", "proved_equivalent": "EQ"}.get(p["verdict"], "UNDECIDED")
    out = {"v": v, "detail": p, "secs": round(time.time() - t0, 2), "stop": r["stop"]}
    if v == "NEQ":
        out["cert"] = r["certificates"]["dbs"][p["cert"]]
    return out


# ------------------------------------------------------------------ adjudication (engine, Σ_P)
def _decode_poly(S, cex):
    import datetime as dt
    import decimal
    db = {t: [] for t in S.order}
    for t, rows in cex.items():
        tb = S.tables[t.lower()]
        for r in rows:
            row = {}
            for c in tb.columns:
                v = r.get(c.name, r.get(c.name.lower()))
                if v is not None:
                    if c.type == "date":
                        v = dt.date.fromisoformat(str(v))
                    elif c.type == "dec":
                        v = decimal.Decimal(v)
                    elif c.type == "str":
                        v = str(v)
                row[c.name] = v
            db[t.lower()].append(row)
    return db


def separates(S, db, q1, q2):
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from seer2.backends import make_backend
    from seer2.refine import Prepared, execute, split_class
    from seer2.backends import EngineRejected
    be = make_backend("duckdb", S, 10.0)
    try:
        inst = be.open(db)
    except EngineRejected as e:
        return None, {"load": str(e)[:200]}
    try:
        o = {"a": execute(inst, Prepared(q1, "duckdb", be.dialect)), "b": execute(inst, Prepared(q2, "duckdb", be.dialect))}
    finally:
        inst.close()
    if any(x.kind != "ok" for x in o.values()):
        return None, {q: o[q].msg for q in o if o[q].kind != "ok"}
    return len(split_class(["a", "b"], o)) > 1, None


def adjudicate(job, k, P, Q):
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from seer.core import from_jsonable
    S = restricted_schema(job["ddl"])
    if P["v"] == "NEQ" and Q["v"] == "EQ":
        try:
            db = _decode_poly(S, P.get("cex") or {})
        except Exception as e:
            return {"winner": "seer", "why": f"polygon cex not decodable: {type(e).__name__}: {e}"}
        viol = S.violations(db)
        if viol:
            return {"winner": "seer", "why": f"polygon cex violates Σ_P: {str(viol[:2])[:200]}"}
        sep, err = separates(S, db, job["q1"], job["q2"])
        if err:
            return {"winner": "seer", "why": f"polygon cex not usable on the engine (load or query error): {err}"}
        return {"winner": "polygon" if sep else "seer",
                "why": "engine confirms polygon cex (SEER missed it)" if sep else "polygon cex does not separate on the engine"}
    if P["v"] == "EQ" and Q["v"] == "NEQ":
        db = from_jsonable(Q["cert"])
        size = max((len(v) for v in db.values()), default=0)
        viol = S.violations(db)
        sep, err = separates(S, db, job["q1"], job["q2"])
        if viol or err or not sep:
            return {"winner": "polygon", "why": f"SEER witness not reproduced: viol={bool(viol)} err={err} sep={sep}"}
        if size > k:
            return {"winner": "none", "why": f"SEER witness has {size} rows/table > bound {k}"}
        return {"winner": "seer", "why": f"engine confirms SEER witness ({size} rows/table <= {k}); Polygon missed it"}
    return None


# ------------------------------------------------------------------ driver
def child(side, k, budget, mem_gb):
    _cap(mem_gb)
    job = json.loads(sys.stdin.read())
    res = run_polygon(job, k, budget) if side == "polygon" else run_seer(job, k, budget)
    print("@@RESULT " + json.dumps(res, default=str), flush=True)


def spawn(py, side, job, a, env_extra):
    # thread/arena caps must be in the environment at process start (glibc reads MALLOC_ARENA_MAX at init);
    # without them thread creation fails under the RLIMIT_AS cap on many-core hosts
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
               MKL_NUM_THREADS="1", MALLOC_ARENA_MAX="2", **env_extra)
    t0 = time.time()
    try:
        p = subprocess.run([py, __file__, "--child", side, "--k", str(a.k), "--budget", str(a.budget),
                            "--mem-gb", str(a.mem_gb)], input=json.dumps(job), capture_output=True, text=True,
                           timeout=a.budget * 3 + 60, env=env)
        line = [l for l in p.stdout.splitlines() if l.startswith("@@RESULT ")]
        out = json.loads(line[-1][9:]) if line else {"v": "CRASH", "rc": p.returncode,
                                                         "reason": (p.stderr or p.stdout or "")[-400:]}
    except subprocess.TimeoutExpired:
        out = {"v": "HARD_TIMEOUT"}
    out["wall"] = round(time.time() - t0, 2)
    return out


def one(job, a):
    S = restricted_schema(job["ddl"])
    pj = dict(job, poly=polygon_input(S))
    P = spawn(a.polygon_py, "polygon", pj, a, {"PATH": os.path.dirname(a.polygon_py) + os.pathsep + os.environ["PATH"]})
    Q = spawn(sys.executable, "seer", job, a, {})
    for _ in range(2):  # a child that dies without output (e.g. fork/limit failure under load) is retried
        if P["v"] == "CRASH" and not P.get("reason"):
            P = spawn(a.polygon_py, "polygon", pj, a, {"PATH": os.path.dirname(a.polygon_py) + os.pathsep + os.environ["PATH"]})
        if Q["v"] == "CRASH" and not Q.get("reason"):
            Q = spawn(sys.executable, "seer", job, a, {})
    rec = {"id": job["id"], "label": job["label"], "polygon": P, "seer": Q}
    if P["v"] in ("EQ", "NEQ") and Q["v"] in ("EQ", "NEQ"):
        rec["agree"] = P["v"] == Q["v"]
        if not rec["agree"]:
            rec["adjudication"] = adjudicate(job, a.k, P, Q)
    return rec


def summarize(path):
    rows = [json.loads(l) for l in open(path)]
    both = [r for r in rows if "agree" in r]
    print(f"pairs {len(rows)}; both decided {len(both)}; agree {sum(r['agree'] for r in both)}")
    print("polygon verdicts", dict(collections.Counter(r["polygon"]["v"] for r in rows)))
    print("seer verdicts   ", dict(collections.Counter(r["seer"]["v"] for r in rows)))
    print("joint           ", dict(collections.Counter((r["polygon"]["v"], r["seer"]["v"]) for r in rows)))
    for r in both:
        if not r["agree"]:
            ad = r.get("adjudication") or {}
            print(f"  DISAGREE {r['id']} label={r['label']} polygon={r['polygon']['v']} seer={r['seer']['v']}"
                  f" -> {ad.get('winner')}: {ad.get('why')}")
    print("winner tally", dict(collections.Counter((r.get("adjudication") or {}).get("winner")
                                                   for r in both if not r["agree"])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="all", choices=["tpch", "cases", "all"])
    ap.add_argument("--out")
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--budget", type=float, default=120)
    ap.add_argument("--mem-gb", type=float, default=8)
    ap.add_argument("--polygon-py", default=sys.executable)
    ap.add_argument("--child", choices=["polygon", "seer"])
    ap.add_argument("--summarize")
    a = ap.parse_args()
    if a.child:
        return child(a.child, a.k, a.budget, a.mem_gb)
    if a.summarize:
        return summarize(a.summarize)
    jobs = pairs(a.suite)
    if a.only:
        keep = set(a.only.split(","))
        jobs = [j for j in jobs if j["id"] in keep]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(a.workers) as ex, open(a.out, "w") as f:
        for i, rec in enumerate(ex.map(lambda j: one(j, a), jobs)):
            f.write(json.dumps(rec, default=str) + "\n")
            f.flush()
            print(i + 1, len(jobs), rec["id"], rec["polygon"]["v"], rec["seer"]["v"], rec.get("agree"), flush=True)
    summarize(a.out)


if __name__ == "__main__":
    main()
