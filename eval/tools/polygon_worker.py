"""Polygon (PLDI'25, Zenodo 10.5281/zenodo.15048096), unmodified source on PYTHONPATH.

op=pair : env.check(q1, q2), bound 2 and k=16 for every operator (author settings of evaluation/calcite/online.py,
          as in src/run_polygon.py), time budget = request timeout.
op=multi: env.disambiguate(queries) with the author's evaluation/disambiguation driver loop: start at bound 1
          (2 if a query has AVG or DISTINCT), k = bound, deepen the bound until a split is found or the budget is
          spent. One adaptation: group_range = len(queries), i.e. any non-empty 2-way split is accepted (the author
          driver uses 0, which demands two equal halves; our task needs any split).
Constraints: PK/FK from PKeys/FKeys (Polygon adds them itself; a composite FK reaches Polygon as one single-column
inclusion per column, its real capability) plus the constraint text, parsed by Polygon's own ConstraintParser
statement by statement; statements it cannot parse are reported in `unsupported_constraints`.
Request: {tables, constraints, op, queries, timeout}."""
import logging, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wproto import serve, header_rows_to_instance  # noqa: E402

OPS = ("filter", "project", "union", "inner join", "left join", "right join", "full join", "product", "order by")


def parse_constraints(c):
    from sql_testing.constraint_parser import ConstraintParser
    p = ConstraintParser()
    if isinstance(c, str) and c.strip() and re.match(r"^\s*\w+\s*:", c):  # LeetCode .yml text
        import yaml
        try:
            c = yaml.safe_load(c)
        except Exception:
            pass
    if isinstance(c, dict):
        pieces = [v for v in c.values() if isinstance(v, str)]
    else:
        pieces = [x for x in re.split(r";|\n", c or "") if x.strip()]
    out, bad = [], []
    for s in pieces:
        try:
            out.extend(p.parse(s.strip()))
        except Exception:
            bad.append(s.strip()[:120])
    return out, bad


def handle(r):
    from sql_testing.environment import Environment
    from sql_testing.logger import logger
    logger.setLevel(logging.ERROR)
    cons, bad = parse_constraints(r.get("constraints"))
    qs = r["queries"]
    base = {"unsupported_constraints": bad[:20], "n_unsupported_constraints": len(bad)}
    t0 = time.time()
    if r["op"] == "pair":
        bnd = int(r.get("bound", 2))      # 2 = author setting; restarts of the evaluation shell deepen it
        env = Environment(r["tables"], cons, bound=bnd, default_k={o: 16 for o in OPS}, time_budget=int(r["timeout"]))
        try:
            eq, cex, _, total, ret = env.check(qs[0], qs[1], use_precise_encoding=False)
        except Exception as e:
            return {**base, "verdict": "unsupported", "reason": f"{type(e).__name__}: {str(e)[:200]}"}
        status = ret.get("status") if isinstance(ret, dict) else None
        if eq is False:
            return {**base, "verdict": "neq", "instance": header_rows_to_instance(cex), "status": status, "bound": bnd}
        return {**base, "verdict": {True: "eq"}.get(eq, "timeout" if status == "TMO" else "unsupported"),
                "status": status}
    # multi
    low = " ".join(qs).lower()
    bound = 2 if ("avg" in low or "distinct" in low) else 1
    last = None
    while time.time() - t0 < r["timeout"]:
        left = r["timeout"] - (time.time() - t0)
        if left < 1:
            break
        env = Environment(r["tables"], cons, bound=bound, default_k={o: bound for o in OPS}, time_budget=int(left))
        try:
            eq, cex, _, total, ret = env.disambiguate(qs, group_range=len(qs), use_precise_encoding=False)
        except Exception as e:
            return {**base, "verdict": "unsupported", "reason": f"{type(e).__name__}: {str(e)[:200]}", "bound": bound}
        last = ret.get("status") if isinstance(ret, dict) else None
        if eq is False:
            return {**base, "verdict": "neq", "instance": header_rows_to_instance(cex), "status": last, "bound": bound}
        if last == "ERR":
            return {**base, "verdict": "unsupported", "status": last, "bound": bound}
        bound += 1
    return {**base, "verdict": "timeout" if last in (None, "TMO") else "eq", "status": last, "bound": bound}


serve(handle)
