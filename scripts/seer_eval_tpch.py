"""Score SEER TPC-H runs against runs/labels.jsonl and independently re-check every split certificate.

Each verdict for the pair (reference, rewrite) is compared with the rewrite's label (eq_unconstrained /
eq_only_constraints / neq / error). For every split, the stored database is reloaded into a fresh engine with all
declared constraints and the two queries must give different outputs there (seer2 execute: bag, or list when both
are ordered and determinate); Schema.violations must be empty.

usage: python scripts/seer_eval_tpch.py runs/server/seer_sys_s0.jsonl [more.jsonl ...]
"""
import collections
import json
import os
import statistics
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
from seer2.backends import make_backend          # noqa: E402
from seer2.refine import Prepared, execute, split_class  # noqa: E402
from seer2.schema import Schema                  # noqa: E402
from seer.core import from_jsonable              # noqa: E402


def score(path, S, labels, sqls):
    be = make_backend("duckdb", S, 30)
    runs = [json.loads(l) for l in open(path) if l.strip()]
    tally = collections.Counter()
    false_split, missed, err_mis, nonrepro, viol = [], [], [], [], 0
    unsup = collections.Counter()
    stops, walls, certs, timing, stats = collections.Counter(), [], 0, collections.Counter(), collections.Counter()
    for res in runs:
        b = res["batch"]
        stops[res["stop"]] += 1
        walls.append(res.get("wall") or 0.0)
        certs += len(res["certificates"]["dbs"])
        for k, v in res.get("timing", {}).items():
            timing[k] += v
        for k, v in res.get("stats", {}).items():
            stats[k] += v
        for q, (code, *_rest) in res.get("unsupported", {}).items():
            unsup[code] += 1
        for rid in res["queries"]:
            if rid == "ref":
                continue
            v = res["verdicts"].get(f"ref|{rid}") or res["verdicts"][f"{rid}|ref"]
            lab = labels[(b, rid)]["label"]
            verdict = v["verdict"]
            if verdict == "undecided":
                verdict = f"undecided:{v['reason'].split(':')[0]}"
            tally[(lab, verdict)] += 1
            key = f"Q{b}.{rid}"
            if lab.startswith("eq") and v["verdict"] == "split":
                false_split.append(key)
            if lab == "neq" and v["verdict"] != "split":
                missed.append((key, verdict))
            if (lab == "error") != (v.get("reason") == "query_error"):
                err_mis.append((key, lab, verdict))
            if v["verdict"] != "split":
                continue
            db = from_jsonable(res["certificates"]["dbs"][v["cert"]])
            if S.violations(db):
                viol += 1
            inst = be.open(db)
            try:
                outs = {"a": execute(inst, Prepared(sqls[(b, "ref")], "duckdb", be.dialect)),
                        "b": execute(inst, Prepared(sqls[(b, rid)], "duckdb", be.dialect))}
            finally:
                inst.close()
            if len(split_class(["a", "b"], outs)) < 2:
                nonrepro.append(key)
    n = sum(tally.values())
    print(f"== {path}")
    print(f"groups {len(runs)}  rewrites {n}  stop {dict(stops)}")
    print(f"wall per group: median {statistics.median(walls):.1f}s  max {max(walls):.1f}s  total {sum(walls):.0f}s")
    print("label x verdict:", {f"{l}->{v}": c for (l, v), c in sorted(tally.items())})
    neq = sum(c for (l, _), c in tally.items() if l == "neq")
    print(f"neq split {neq - len(missed)}/{neq}  missed {missed}")
    print(f"FALSE SPLITS (eq-labelled but split): {len(false_split)} {false_split}")
    print(f"error-label mismatches: {err_mis}")
    print(f"certificates: {certs} stored; pair certificates re-checked independently: "
          f"{sum(c for (l, v), c in tally.items() if v == 'split')}, Σ violations {viol}, not reproducing {nonrepro}")
    print(f"unsupported queries by reason (incl. references): {dict(unsup)}")
    print("timing totals (s):", {k: round(v, 1) for k, v in sorted(timing.items())})
    print("counts:", dict(sorted(stats.items())))
    return {"false_split": len(false_split), "missed": len(missed), "nonrepro": len(nonrepro), "viol": viol}


def main(paths):
    S = Schema(open(os.path.join(ROOT, "data/tpch_schema.sql")).read())
    labels = {(str(r["qid"]), str(r["rid"])): r for r in map(json.loads, open(os.path.join(ROOT, "runs/labels.jsonl")))}
    sqls = {(str(r["batch"]), str(r["id"])): r["sql"]
            for r in map(json.loads, open(os.path.join(ROOT, "data/seer2_tpch_batch.jsonl")))}
    for p in paths:
        score(p, S, labels, sqls)


if __name__ == "__main__":
    main(sys.argv[1:])
