"""Polygon (PLDI'25, Zenodo 10.5281/zenodo.15048096) pairwise refutation `env.check(original, rewrite)` on every
TPC-H rewrite, with the TPC-H integrity constraints given to Polygon in its own constraint language:
  PK  -> schema PKeys (Polygon adds {'primary': ...}: distinct + not null)
  FK  -> schema FKeys (single-column inclusion {'eq': [child.col, parent.col]}).  The composite FK
         lineitem(l_partkey, l_suppkey) -> partsupp is not expressible; it is given as two single-column
         inclusions, which is weaker (counted when the verifier finds a violating counterexample).
  NOT NULL -> {'not_null': 'table.col'} for every column.
Author settings from evaluation/calcite/online.py: bound=2, default_k=16 for every operator; time budget per pair =
120 s (same as ParSEval's effective per-pair budget in runs/parseval.jsonl, 2 x GEN_LIMIT_S=60).

Runs in Polygon's venv with Polygon's source on PYTHONPATH:
  PATH=~/code/polygon-pldi25-zenodo/.venv/bin:$PATH PYTHONPATH=~/code/polygon-pldi25-zenodo/src/polygon-artifact-evaluation \
    python src/run_polygon.py runs/labels.jsonl runs/polygon.jsonl --workers 10
Counterexamples are verified separately (src/verify.py) in DuckDB against the full constraints.
"""
import argparse, json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUERIES = json.load(open(f"{ROOT}/data/tpch_queries.json"))
BUDGET_S = 120
HARD_S = 400
BOUND = 2
K = 16

# column types as Polygon understands them (all cells are SMT Ints; strings hashed, dates = days since 1000-01-01)
T = {"int": "int", "dec": "numeric", "str": "varchar", "date": "date"}
SCHEMA = {
    "region": [("r_regionkey", "int"), ("r_name", "str"), ("r_comment", "str")],
    "nation": [("n_nationkey", "int"), ("n_name", "str"), ("n_regionkey", "int"), ("n_comment", "str")],
    "part": [("p_partkey", "int"), ("p_name", "str"), ("p_mfgr", "str"), ("p_brand", "str"), ("p_type", "str"),
             ("p_size", "int"), ("p_container", "str"), ("p_retailprice", "dec"), ("p_comment", "str")],
    "supplier": [("s_suppkey", "int"), ("s_name", "str"), ("s_address", "str"), ("s_nationkey", "int"),
                 ("s_phone", "str"), ("s_acctbal", "dec"), ("s_comment", "str")],
    "partsupp": [("ps_partkey", "int"), ("ps_suppkey", "int"), ("ps_availqty", "int"), ("ps_supplycost", "dec"),
                 ("ps_comment", "str")],
    "customer": [("c_custkey", "int"), ("c_name", "str"), ("c_address", "str"), ("c_nationkey", "int"),
                 ("c_phone", "str"), ("c_acctbal", "dec"), ("c_mktsegment", "str"), ("c_comment", "str")],
    "orders": [("o_orderkey", "int"), ("o_custkey", "int"), ("o_orderstatus", "str"), ("o_totalprice", "dec"),
               ("o_orderdate", "date"), ("o_orderpriority", "str"), ("o_clerk", "str"), ("o_shippriority", "int"),
               ("o_comment", "str")],
    "lineitem": [("l_orderkey", "int"), ("l_partkey", "int"), ("l_suppkey", "int"), ("l_linenumber", "int"),
                 ("l_quantity", "dec"), ("l_extendedprice", "dec"), ("l_discount", "dec"), ("l_tax", "dec"),
                 ("l_returnflag", "str"), ("l_linestatus", "str"), ("l_shipdate", "date"), ("l_commitdate", "date"),
                 ("l_receiptdate", "date"), ("l_shipinstruct", "str"), ("l_shipmode", "str"), ("l_comment", "str")],
}
PK = {"region": ["r_regionkey"], "nation": ["n_nationkey"], "part": ["p_partkey"], "supplier": ["s_suppkey"],
      "partsupp": ["ps_partkey", "ps_suppkey"], "customer": ["c_custkey"], "orders": ["o_orderkey"],
      "lineitem": ["l_orderkey", "l_linenumber"]}
FK1 = {"nation": [("n_regionkey", "region", "r_regionkey")], "supplier": [("s_nationkey", "nation", "n_nationkey")],
       "customer": [("c_nationkey", "nation", "n_nationkey")],
       "partsupp": [("ps_partkey", "part", "p_partkey"), ("ps_suppkey", "supplier", "s_suppkey")],
       "orders": [("o_custkey", "customer", "c_custkey")],
       "lineitem": [("l_orderkey", "orders", "o_orderkey"),
                    # composite FK split into two single-column inclusions (Polygon cannot express the pair)
                    ("l_partkey", "partsupp", "ps_partkey"), ("l_suppkey", "partsupp", "ps_suppkey")]}
ORDER = list(SCHEMA)


def polygon_schema():
    idx = {t: i for i, t in enumerate(ORDER)}
    out = []
    for t in ORDER:
        ty = dict(SCHEMA[t])
        out.append({"TableName": t,
                    "PKeys": [{"Name": c, "Type": T[ty[c]]} for c in PK[t]],
                    "FKeys": [{"FName": c, "PName": pc, "PTable": str(idx[p])} for c, p, pc in FK1.get(t, [])],
                    "Others": [{"Name": c, "Type": T[k]} for c, k in SCHEMA[t] if c not in PK[t]]})
    cons = [{"not_null": f"{t}.{c}"} for t in ORDER for c, _ in SCHEMA[t]]
    return out, cons


def one_inproc(q1, q2):
    import logging
    from sql_testing.environment import Environment
    from sql_testing.logger import logger
    logger.setLevel(logging.ERROR)
    schema, cons = polygon_schema()
    k = {op: K for op in ("filter", "project", "union", "inner join", "left join", "right join", "full join",
                          "product", "order by")}
    t = time.time()
    env = Environment(schema, cons, bound=BOUND, default_k=k, time_budget=BUDGET_S)
    # Diagnostic only: Polygon's own parse+encode of each query, to record WHY an ERR happens (errors raised inside
    # env.check's worker process are swallowed). Polygon's code is not modified; env.check below is the verdict.
    reason, i = "", 0
    try:
        import traceback
        from sql_testing.sql_parser import SQLParser
        from sql_testing.visitors.query_encoder import QueryEncoder
        denv = Environment(schema, cons, bound=BOUND, default_k=k, time_budget=BUDGET_S)
        p = SQLParser()
        for i, s in enumerate((q1, q2)):
            denv.curr_query_id = i
            p.parse_query(p.parse(s)).accept(QueryEncoder(denv))
    except Exception as e:
        first = (str(e).splitlines() or [""])[0][:200]
        reason = f"{'orig' if i == 0 else 'rewrite'} encode: {type(e).__name__}: {first}"
    t = time.time()
    try:
        eq, cex, _, total, ret = env.check(q1, q2, use_precise_encoding=False)
    except Exception as e:  # e.g. Polygon's SQL parser raising outside its worker process
        return {"verdict": "ERR", "status": "EXC", "secs": round(time.time() - t, 2), "polygon_total": None,
                "reason": reason or f"check raised {type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}"}
    status = ret.get("status") if isinstance(ret, dict) else None
    verdict = {True: "EQ", False: "NEQ"}.get(eq, "TIMEOUT" if status == "TMO" else "ERR")
    out = {"verdict": verdict, "status": status, "secs": round(time.time() - t, 2), "polygon_total": total}
    if verdict == "ERR":
        out["reason"] = reason or "error after encoding (solver/search)"
    if cex:
        out["cex"] = {t_: [dict(zip(rows[0], r)) for r in rows[1:]] for t_, rows in cex.items()}
    return out


def one(job):
    r = job
    t = time.time()
    try:
        p = subprocess.run([sys.executable, __file__, "--one", str(r["qid"]), str(r["rid"])],
                           capture_output=True, text=True, timeout=HARD_S, input=json.dumps(r["sql"]))
        lines = [l for l in p.stdout.splitlines() if l.startswith("{\"verdict\"")]
        out = json.loads(lines[-1]) if lines else {"verdict": "CRASH", "reason": p.stderr[-600:]}
    except subprocess.TimeoutExpired:
        out = {"verdict": "HARD_TIMEOUT", "secs": HARD_S}
    out["wall"] = round(time.time() - t, 2)
    return {"qid": r["qid"], "rid": r["rid"], "label": r["label"], **out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", nargs="?"); ap.add_argument("out", nargs="?")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--one", nargs=2)
    ap.add_argument("--only", default="", help="comma list qid.rid")
    a = ap.parse_args()
    if a.one:
        sql = json.loads(sys.stdin.read())
        print(json.dumps(one_inproc(QUERIES[a.one[0]], sql), default=str), flush=True)
        return
    rows = [json.loads(l) for l in open(a.labels)]
    rows = [{k: r[k] for k in ("qid", "rid", "label", "sql")} for r in rows if r["label"] != "error"]
    if a.only:
        keep = set(a.only.split(","))
        rows = [r for r in rows if f"{r['qid']}.{r['rid']}" in keep]
    with ThreadPoolExecutor(a.workers) as ex, open(a.out, "w") as f:
        for i, res in enumerate(ex.map(one, rows)):
            f.write(json.dumps(res, default=str) + "\n"); f.flush()
            print(i, len(rows), res["qid"], res["rid"], res["verdict"], res.get("secs"), flush=True)


if __name__ == "__main__":
    main()
