"""python -m seer2 --schema X.sql --batch batch.jsonl --backend duckdb --budget 60 --seed 0 --out out.jsonl

batch.jsonl: one JSON object per line with "sql" and optional "id", "batch" (group key; one refinement per group)
and "reference": true (at most one per group). Output: one JSON line per group (see Refiner.result).
"""
import argparse
import json
import os
import resource
import sys

# one thread per native library: with an address-space cap, per-core thread pools (OpenBLAS / OpenMP / malloc arenas)
# on a many-core server exhaust the cap at start-up ("cannot allocate memory for thread-local data").
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
os.environ.setdefault("MALLOC_ARENA_MAX", "2")

from .refine import Refiner
from .schema import Schema


def main(argv=None):
    ap = argparse.ArgumentParser(prog="seer2")
    ap.add_argument("--schema", required=True)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--backend", default="duckdb", choices=["duckdb", "sqlite", "postgres"])
    ap.add_argument("--dialect", default="duckdb", help="dialect of the DDL and the queries")
    ap.add_argument("--budget", type=float, default=60.0, help="wall-clock seconds per batch")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--M", type=int, default=4, help="candidate instances per round")
    ap.add_argument("--delta", type=float, default=0.05)
    ap.add_argument("--groups", default="", help="comma-separated batch keys to run (default: all)")
    ap.add_argument("--mem-gb", type=float, default=8.0, help="address-space cap for this process")
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.mem_gb > 0 and sys.platform.startswith("linux"):
        lim = int(a.mem_gb * 2 ** 30)
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
    schema = Schema(open(a.schema).read(), a.dialect)
    groups = {}
    for i, line in enumerate(open(a.batch)):
        if not line.strip():
            continue
        r = json.loads(line)
        groups.setdefault(str(r.get("batch", "all")), []).append(r)
    keys = [k for k in groups if not a.groups or k in a.groups.split(",")]
    with open(a.out, "w") as f:
        for k in keys:
            rows = groups[k]
            qs = [(str(r.get("id", j)), r["sql"]) for j, r in enumerate(rows)]
            ref = next((str(r.get("id", j)) for j, r in enumerate(rows) if r.get("reference")), None)
            rf = Refiner(schema, qs, backend=a.backend, dialect=a.dialect, budget=a.budget, seed=a.seed, M=a.M,
                         delta=a.delta, reference=ref, trace=print if a.trace else None)
            res = rf.run()
            res["batch"] = k
            res["seed"] = a.seed
            f.write(json.dumps(res, default=str) + "\n")
            f.flush()
            print(k, res["stop"], res["wall"], "classes:", len(res["classes"]), "dbs:", res["n_db"],
                  "errors:", len(res["error"]), flush=True)


if __name__ == "__main__":
    main()
