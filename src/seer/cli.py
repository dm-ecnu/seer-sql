"""SEER command line.

  python -m seer batch  --schema X.sql --batch B.jsonl --out R.jsonl [--dialect duckdb] [--backend duckdb] ...
  python -m seer pair   --schema X.sql --q1 "SELECT ..." --q2 "SELECT ..."
  python -m seer replay --schema X.sql --results R.jsonl          (re-executes every stored certificate)

B.jsonl: one JSON object per line with "sql", optional "id" and "batch" (group key; one partition per group).
R.jsonl: one JSON line per group: partition (classes), pairwise verdicts with stop reasons, certificate store
(databases + the refinement each caused), unsupported / query_error queries, per-stage timings and counts.
"""
import argparse
import json
import os
import resource
import sys

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
os.environ.setdefault("MALLOC_ARENA_MAX", "2")


def _limits(mem_gb):
    import z3
    if mem_gb > 0:
        z3.set_param("memory_max_size", int(mem_gb * 1024 * 0.75))
        if sys.platform.startswith("linux"):
            lim = int(mem_gb * 2 ** 30)
            resource.setrlimit(resource.RLIMIT_AS, (lim, lim))


def _common(ap):
    ap.add_argument("--schema", required=True)
    ap.add_argument("--dialect", default="duckdb", help="dialect of the DDL and the queries")
    ap.add_argument("--backend", default="duckdb", choices=["duckdb", "sqlite", "postgres"])
    ap.add_argument("--budget", type=float, default=120.0, help="wall-clock seconds per group")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--levels", default="1,2,4", help="symbolic bounds k (tuples per table)")
    ap.add_argument("--fast-dbs", type=int, default=200)
    ap.add_argument("--no-fast", action="store_true")
    ap.add_argument("--no-symbolic", action="store_true")
    ap.add_argument("--symbolic-selection", choices=["representatives", "max_pairs"], default="representatives")
    ap.add_argument("--solver-timeout-ms", type=int, default=20000)
    ap.add_argument("--max-cost", type=int, default=400000, help="encoding size guard per query and level")
    ap.add_argument("--mem-gb", type=float, default=8.0, help="address-space cap for this process")
    ap.add_argument("--trace", action="store_true")


def _kw(a):
    return dict(dialect=a.dialect, backend=a.backend, budget=a.budget, seed=a.seed,
                levels=tuple(int(x) for x in a.levels.split(",") if x), fast_dbs=a.fast_dbs, fast=not a.no_fast,
                symbolic=not a.no_symbolic, solver_timeout_ms=a.solver_timeout_ms, max_cost=a.max_cost,
                symbolic_selection=a.symbolic_selection,
                trace=print if a.trace else None)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="seer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("batch")
    _common(b)
    b.add_argument("--batch", required=True)
    b.add_argument("--groups", default="", help="comma-separated group keys (default: all)")
    b.add_argument("--out", required=True)
    p = sub.add_parser("pair")
    _common(p)
    p.add_argument("--q1", required=True)
    p.add_argument("--q2", required=True)
    r = sub.add_parser("replay")
    r.add_argument("--schema", required=True)
    r.add_argument("--dialect", default="duckdb")
    r.add_argument("--backend", default="duckdb", choices=["duckdb", "sqlite", "postgres"])
    r.add_argument("--results", required=True)
    r.add_argument("--mem-gb", type=float, default=8.0)
    a = ap.parse_args(argv)
    _limits(a.mem_gb)

    from seer2.schema import Schema
    from .core import Seer, replay
    schema = Schema(open(a.schema).read(), a.dialect)

    if a.cmd == "pair":
        res = Seer(schema, [("q1", a.q1), ("q2", a.q2)], **_kw(a)).run()
        print(json.dumps({"verdict": res["verdicts"]["q1|q2"], "stop": res["stop"], "timing": res["timing"],
                          "symbolic_selection": res["symbolic_selection"],
                          "symbolic_selection_trace": res["symbolic_selection_trace"],
                          "stats": res["stats"], "unsupported": res["unsupported"],
                          "certificate": (res["certificates"]["dbs"][res["verdicts"]["q1|q2"]["cert"]]
                                          if res["verdicts"]["q1|q2"].get("cert") is not None else None)},
                         default=str, indent=1))
        return
    if a.cmd == "replay":
        n = 0
        for line in open(a.results):
            if line.strip():
                n += replay(schema, json.loads(line), dialect=a.dialect, backend=a.backend)
        print(f"replayed {n} certificate events: all reproduced")
        return

    groups = {}
    for line in open(a.batch):
        if line.strip():
            row = json.loads(line)
            groups.setdefault(str(row.get("batch", "all")), []).append(row)
    keys = [k for k in groups if not a.groups or k in a.groups.split(",")]
    with open(a.out, "w") as f:
        for k in keys:
            rows = groups[k]
            qs = [(str(x.get("id", j)), x["sql"]) for j, x in enumerate(rows)]
            try:
                res = Seer(schema, qs, **_kw(a)).run()
            except MemoryError:
                res = {"stop": "memory_cap", "classes": None}
            res["batch"] = k
            res["seed"] = a.seed
            f.write(json.dumps(res, default=str) + "\n")
            f.flush()
            print(k, res["stop"], res.get("wall"), "classes:", len(res["classes"] or []),
                  "certs:", len(res.get("certificates", {}).get("dbs", [])),
                  "unsup:", len(res.get("unsupported", {})), "qerr:", len(res.get("query_error", {})), flush=True)


if __name__ == "__main__":
    main()
