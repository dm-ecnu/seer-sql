"""SEER main table from the per-dataset score files of scripts/matrix_score.py.

  python scripts/main_table.py scores/*.test.json > main_table.md

Rows: SEER, v2, the published opponents, then Polygon (its better mode per dataset: native multi-query or the
pairwise shell) and VeriEQL as reference rows. Cell: non-equivalence recall over the known-inequivalent union,
mean ± std over the seeds (in %), and below it the per-seed mean of undecided pairs that were unsupported (u) or
timed out (t), and the infrastructure-failed batches (i) left after the rerun (missing, not imputed).
"""
import json
import os
import statistics
import sys

ROWS = [("seer", "SEER"), ("v2", "v2"), ("sqlwitness", "SQLWitness"), ("parseval", "ParSEval"),
        ("xdatan", "XDataN"), ("sqldriller_official", "SQLDriller"), ("sqldriller", "SQLDriller (reconstructed; unverified)"), ("polygon*", "Polygon (ref.)"),
        ("verieql", "VeriEQL (ref.)")]
COLS = ["spider_pairs", "bird_pairs", "minidev_pg_pairs", "leetcode_batches", "cubes_d50", "cubes_d100", "tpch",
        "tpcds", "xdatabm", "tpch_mut"]


def cell(d):
    if not d:
        return None
    rows = d["per_seed"]
    rec = [r["recall"] for r in rows]
    mean = lambda k: statistics.mean(r["outcomes"].get(k, 0) for r in rows)
    return {"n": len(rows), "mean": statistics.mean(rec), "std": statistics.stdev(rec) if len(rec) > 1 else 0.0,
            "u": mean("undecided:unsupported"), "t": mean("undecided:timeout"),
            "i": statistics.mean(r.get("infra_batches", 0) for r in rows)}


def main():
    S = {}
    for f in sys.argv[1:]:
        j = json.load(open(f))
        S[j["dataset"]] = j
    cols = [c for c in COLS if c in S]
    table = {}
    for key, _ in ROWS:
        for c in cols:
            ms = S[c]["methods"]
            if key == "polygon*":
                cand = [cell(ms.get(m)) for m in ("polygon", "polygon_pair")]
                cand = [x for x in cand if x]
                table[key, c] = max(cand, key=lambda x: x["mean"]) if cand else None
            else:
                table[key, c] = cell(ms.get(key))
    print("| method | " + " | ".join(f"{c} (known {S[c]['known_neq']})" for c in cols) + " |")
    print("|---" * (len(cols) + 1) + "|")
    for key, name in ROWS:
        out = []
        for c in cols:
            x = table[key, c]
            out.append("—" if x is None else
                       f"{100 * x['mean']:.1f} ± {100 * x['std']:.1f}<br>u {x['u']:.0f} / t {x['t']:.0f} / i {x['i']:.0f}"
                       + ("" if x["n"] == 5 else f" ({x['n']} seeds)"))
        print(f"| {name} | " + " | ".join(out) + " |")
    print()
    print("SEER minus opponent (recall points, mean over seeds):")
    for key, name in ROWS[1:]:
        diffs = []
        for c in cols:
            a, b = table["seer", c], table[key, c]
            if a and b:
                diffs.append(f"{c} {100 * (a['mean'] - b['mean']):+.1f}")
        print(f"- {name}: " + ", ".join(diffs))
    json.dump({f"{k}|{c}": v for (k, c), v in table.items()}, open(os.path.join(os.path.dirname(sys.argv[1]), "main_table.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
