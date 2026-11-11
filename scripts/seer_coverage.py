"""Parse / IR coverage of the SEER front end on external workloads, with Unsupported reasons.

Every query is translated with seer.front.Front. A query that fails is additionally run on the empty database of
its schema by the engine of its dialect (DuckDB / SQLite / PostgreSQL; MySQL has no seer2 backend and is not
screened): failures the engine also rejects are counted as query errors, not as SEER gaps.

  python scripts/seer_coverage.py --eval-root EVAL --batches B1.jsonl[,B2.jsonl] --tpcds --out coverage.json
Common batch format (eval/build/build_datasets.py): {"batch_id","dataset","dialect","schema_path","queries":[...]}.
--tpcds: EVAL/data/tpcds/{tpcds_schema.sql,queries.json,rewrites.jsonl} (DuckDB dialect).
"""
import argparse
import collections
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
from seer2.backends import make_backend          # noqa: E402
from seer2.refine import Prepared, execute       # noqa: E402
from seer2.schema import Schema                  # noqa: E402
from seer.front import Front                     # noqa: E402
from seer.ir import Unsupported                  # noqa: E402

DIALECT = {"postgresql": "postgres", "sqlite": "sqlite", "mysql": "mysql", "duckdb": "duckdb"}
BACKEND = {"postgres": "postgres", "sqlite": "sqlite", "duckdb": "duckdb"}


class Tally:
    def __init__(self):
        self.n = 0
        self.ok = 0
        self.unsup = collections.Counter()
        self.qerr = collections.Counter()
        self.unscreened = collections.Counter()
        self.schema_fail = collections.Counter()
        self.examples = {}

    def as_dict(self):
        gaps = sum(self.unsup.values()) + sum(self.unscreened.values())
        valid = self.n - sum(self.qerr.values()) - sum(self.schema_fail.values())
        return {"queries": self.n, "translated": self.ok, "engine_rejected": sum(self.qerr.values()),
                "schema_failed": sum(self.schema_fail.values()),
                "supported_of_valid": round(self.ok / valid, 4) if valid else None,
                "unsupported_valid": dict(self.unsup.most_common()),
                "unsupported_unscreened": dict(self.unscreened.most_common()),
                "engine_rejected_by_front_code": dict(self.qerr.most_common()),
                "schema_fail_reasons": dict(self.schema_fail.most_common()), "gaps": gaps,
                "examples": self.examples}


def run_queries(T, S, front, dialect, queries, screen):
    inst = None
    for sql in queries:
        T.n += 1
        try:
            front.translate(sql)
            T.ok += 1
            continue
        except Unsupported as u:
            code = u.code
            T.examples.setdefault(code, (u.detail or "")[:160] + " | " + sql[:240].replace("\n", " "))
        except Exception as e:                         # a crash is a bug: count it under its own code
            code = f"CRASH:{type(e).__name__}"
            T.examples.setdefault(code, str(e)[:160] + " | " + sql[:240].replace("\n", " "))
        if not screen:
            T.unscreened[code] += 1
            continue
        if inst is None:
            try:
                inst = screen.open({t: [] for t in S.order})
            except Exception as e:                     # the engine rejects the DDL we emit: cannot screen this schema
                T.examples.setdefault("screen_ddl_failed", str(e)[:160])
                screen = None
                T.unscreened[code] += 1
                continue
        o = execute(inst, Prepared(sql, dialect, screen.dialect))
        (T.qerr if o.kind == "err" else T.unsup)[code] += 1
    if inst is not None:
        inst.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-root", required=True)
    ap.add_argument("--batches", default="")
    ap.add_argument("--tpcds", action="store_true")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    sys.path.insert(0, a.eval_root)
    from seereval.schema import Schema as ESchema, to_ddl
    report = {}
    for path in [p for p in a.batches.split(",") if p]:
        T = Tally()
        cache = {}
        base = os.path.dirname(path)
        seen = set()
        for line in open(path):
            b = json.loads(line)
            d = DIALECT[b["dialect"]]
            qs = [q["sql"] for q in b["queries"] if q["sql"] not in seen]
            seen.update(qs)
            key = b["schema_path"]
            if key not in cache:
                try:
                    es = ESchema.load(os.path.join(base, key))
                    ddl = ";\n".join(to_ddl(es, b["dialect"], deferrable=False)) + ";"
                    S = Schema(ddl, d)
                    be = make_backend(BACKEND[d], S, 10) if d in BACKEND else None
                    cache[key] = (S, Front(S, d), be)
                except Exception as e:
                    cache[key] = f"{type(e).__name__}: {str(e)[:120]}"
            c = cache[key]
            if isinstance(c, str):
                T.n += len(qs)
                T.schema_fail[c.split(":")[0]] += len(qs)
                T.examples.setdefault("schema:" + c.split(":")[0], c)
                continue
            S, front, be = c
            run_queries(T, S, front, d, qs, be)
        report[os.path.basename(path)] = T.as_dict()
    if a.tpcds:
        dd = os.path.join(a.eval_root, "data", "tpcds")
        S = Schema(open(os.path.join(dd, "tpcds_schema.sql")).read(), "postgres")
        front = Front(S, "duckdb")
        be = make_backend("duckdb", S, 10)
        qs = list(json.load(open(os.path.join(dd, "queries.json"))).values())
        T = Tally()
        run_queries(T, S, front, "duckdb", qs, be)
        report["tpcds_queries"] = T.as_dict()
        T = Tally()
        rw = [json.loads(l)["sql"] for l in open(os.path.join(dd, "rewrites.jsonl"))]
        run_queries(T, S, front, "duckdb", rw, be)
        report["tpcds_rewrites"] = T.as_dict()
    json.dump(report, open(a.out, "w"), indent=1)
    for k, v in report.items():
        print(f"{k}: {v['queries']} queries, translated {v['translated']}, engine-rejected {v['engine_rejected']}, "
              f"schema-failed {v['schema_failed']}, supported/valid {v['supported_of_valid']}")
        print("   unsupported (valid):", v["unsupported_valid"])
        if v["unsupported_unscreened"]:
            print("   unsupported (not screened):", v["unsupported_unscreened"])


if __name__ == "__main__":
    main()
