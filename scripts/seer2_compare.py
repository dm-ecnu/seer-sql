"""Compare a seer2 TPC-H run with runs/labels.jsonl and independently re-check every NEQ certificate: the
serialized instance is loaded into a fresh DuckDB with all declared constraints (PK/UNIQUE/FK/NOT NULL/CHECK),
Schema.violations must be empty, and the reference and the rewrite must give different bags.

usage: python scripts/seer2_compare.py runs/seer2_smoke_s0.jsonl
"""
import collections
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from bounded import norm  # noqa: E402
from seer2 import Schema  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def bag(rows):
    return collections.Counter(tuple(norm(v) for v in r) for r in rows)


def main(path):
    import duckdb
    S = Schema(open(os.path.join(ROOT, "data/tpch_schema.sql")).read())
    ddl = S.ddl_for("duckdb", deferred_fk=False)
    labels = {(r["qid"], r["rid"]): r for r in map(json.loads, open(os.path.join(ROOT, "runs/labels.jsonl")))}
    sqls = {}
    for r in map(json.loads, open(os.path.join(ROOT, "data/seer2_tpch_batch.jsonl"))):
        sqls[(str(r["batch"]), str(r["id"]))] = r["sql"]
    runs = [json.loads(l) for l in open(path) if l.strip()]
    tally = collections.Counter()
    neq_split, neq_missed, spurious, err_mis, viol, uncert = [], [], [], [], 0, []
    walls, stops = [], collections.Counter()
    for res in runs:
        b = res["batch"]
        walls.append(res["wall"])
        stops[res["stop"]] += 1
        for rid, v in res["verdicts"].items():
            lab = labels[(int(b), int(rid))]["label"]
            tally[(lab, v["verdict"])] += 1
            key = f"Q{b}.{rid}"
            if lab == "neq":
                (neq_split if v["verdict"] == "NEQ" else neq_missed).append((key, v["verdict"]))
            elif lab.startswith("eq") and v["verdict"] == "NEQ":
                spurious.append(key)
            if (lab == "error") != (v["verdict"] == "ERROR"):
                err_mis.append((key, lab, v["verdict"]))
            if v["verdict"] != "NEQ":
                continue
            db = res["dbs"][str(v["db"])]
            if S.violations(_typed(S, db)):
                viol += 1
            con = duckdb.connect()
            for st in ddl:
                con.execute(st)
            for t in S.order:
                rows = db.get(t, [])
                if rows:
                    cols = S.tables[t].colnames
                    con.executemany(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                    [[r.get(c) for c in cols] for r in rows])
            a = bag(con.execute(sqls[(b, "ref")]).fetchall())
            c = bag(con.execute(sqls[(b, rid)]).fetchall())
            con.close()
            if a == c:
                uncert.append(key)
    print(f"batches {len(runs)}  stop {dict(stops)}  wall median {statistics.median(walls):.1f}s "
          f"max {max(walls):.1f}s  total {sum(walls):.0f}s")
    print("label x verdict:", {f"{l}->{v}": n for (l, v), n in sorted(tally.items())})
    print(f"neq split {len(neq_split)}/{len(neq_split) + len(neq_missed)}  missed {neq_missed}")
    print(f"spurious NEQ on eq-labelled rewrites: {len(spurious)} {spurious}")
    print(f"ERROR mismatches vs label: {err_mis}")
    print(f"certificates re-checked {sum(n for (l, v), n in tally.items() if v == 'NEQ')}: "
          f"constraint violations {viol}, not reproducing on a fresh connection {len(uncert)} {uncert}")


def _typed(S, db):
    """JSON instance -> python values for Schema.violations (dates stay ISO strings; comparisons are within column)."""
    return {t: [dict(r) for r in rows] for t, rows in db.items()}


if __name__ == "__main__":
    main(sys.argv[1])
