"""Encoding cost per TPC-H group and bound k: one symbolic.Level over all encodable queries of the group, with the
same per-query cap the system uses at budget 120 (10 s). Prints one JSON line per (group, k).

usage: python scripts/seer_encode_bench.py data/seer2_tpch_batch.jsonl data/tpch_schema.sql 1,2,4 [groups]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from seer2.schema import Schema                  # noqa: E402
from seer.front import Front                     # noqa: E402
from seer.encode import Domain, schema_strings                   # noqa: E402
from seer.ir import Unsupported                  # noqa: E402
from seer.symbolic import Level                  # noqa: E402


def main():
    batch, schema, ks = sys.argv[1], sys.argv[2], [int(x) for x in sys.argv[3].split(",")]
    only = set(sys.argv[4].split(",")) if len(sys.argv) > 4 else None
    S = Schema(open(schema).read())
    F = Front(S, "duckdb")
    groups = {}
    for line in open(batch):
        r = json.loads(line)
        groups.setdefault(str(r["batch"]), []).append(r)
    for g, rows in groups.items():
        if only and g not in only:
            continue
        irs = {}
        for r in rows:
            try:
                irs[r["id"]] = F.translate(r["sql"])
            except Unsupported:
                pass
        dom = Domain.build(list(irs.values()), checks_strings=schema_strings(S))
        for k in ks:
            t0 = time.perf_counter()
            lv = Level(S, F, dom, irs, k, "duckdb", deadline=t0 + 120, per_query_s=10.0)
            print(json.dumps({"group": g, "k": k, "encode_s": round(lv.encode_s, 3), "queries": len(irs),
                              "encoded": len(lv.out), "unsup": lv.unsup}), flush=True)


if __name__ == "__main__":
    main()
