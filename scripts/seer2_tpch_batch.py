"""Build the seer2 batch file for the 22 TPC-H batches (reference query + its 10 LLM rewrites)."""
import json
import sys

root = sys.argv[1] if len(sys.argv) > 1 else "."
Q = json.load(open(f"{root}/data/tpch_queries.json"))
out = open(sys.argv[2] if len(sys.argv) > 2 else f"{root}/data/seer2_tpch_batch.jsonl", "w")
for qid in sorted(Q, key=int):
    out.write(json.dumps({"batch": qid, "id": "ref", "reference": True, "sql": Q[qid]}) + "\n")
for line in open(f"{root}/data/rewrites.jsonl"):
    r = json.loads(line)
    out.write(json.dumps({"batch": str(r["qid"]), "id": str(r["rid"]), "sql": r["sql"]}) + "\n")
