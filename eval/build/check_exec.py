"""Sanity check of the converted datasets: every query must execute on the dialect's engine over the empty
constrained schema emitted from our normalized schema JSON (catches DDL conversion errors: column order, names,
types). Prints per-dataset counts of failing queries and a few examples.

  python eval/build/check_exec.py DATASET.jsonl [--limit N]
"""
import argparse, json, os, sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from seereval.schema import Schema  # noqa: E402
from seereval.validator import empty_db_errors  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("path"); ap.add_argument("--limit", type=int, default=0)
a = ap.parse_args()
root = os.path.dirname(os.path.abspath(a.path))
n = bad = nb = 0
ex = Counter()
roles = Counter()
for i, line in enumerate(open(a.path)):
    if a.limit and i >= a.limit:
        break
    b = json.loads(line)
    s = Schema.load(os.path.join(root, b["schema_path"]))
    errs = empty_db_errors(s, b["dialect"], {q["qid"]: q["sql"] for q in b["queries"]}, timeout_s=20)
    nb += 1
    n += len(b["queries"])
    bad += len(errs)
    for q in b["queries"]:
        if q["qid"] in errs:
            roles[q["role"]] += 1
            ex[errs[q["qid"]][:110]] += 1
print(json.dumps({"file": os.path.basename(a.path), "batches": nb, "queries": n, "fail_on_empty_schema": bad,
                  "fail_by_role": dict(roles), "top_errors": ex.most_common(6)}, indent=1))
