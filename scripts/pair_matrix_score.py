"""Score isolated pair-evidence runs after independently replaying every claimed witness.

This reads only pair-state records. Missing, crashed, query-error and unresolved
pairs remain distinct; no partition or transitivity inference is made.
"""
import argparse
import collections
import hashlib
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from seer.core import from_jsonable
from seereval.schema import Schema
from seereval.validator import check_db, classify_from_check


def dataset(path):
    root = os.path.dirname(os.path.abspath(path))
    batches = {}
    for line in open(path):
        if not line.strip():
            continue
        batch = json.loads(line)
        bid = batch["batch_id"]
        if bid in batches:
            raise ValueError("duplicate dataset batch: " + bid)
        ids = [q["qid"] for q in batch["queries"]]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate query ID: " + bid)
        batch["_schema"] = Schema.load(os.path.join(root, batch["schema_path"]))
        batches[bid] = batch
    return batches


def scored_pairs(batch):
    qs = batch["queries"]
    ref = next((q["qid"] for q in qs if q.get("role") == "reference"), None)
    if ref is not None:
        return [(ref, q["qid"]) for q in qs if q["qid"] != ref]
    return [(a["qid"], b["qid"]) for i, a in enumerate(qs) for b in qs[i + 1:]
            if a["sql"].strip() != b["sql"].strip()]


def load_records(path):
    records = {}
    contract = None
    for line in open(path):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("state_model") != "pair_evidence" or not row.get("comparison_contract"):
            raise ValueError("requires pair-evidence record with source contract")
        if row["batch_id"] in records:
            raise ValueError("duplicate batch record: " + row["batch_id"])
        if contract is None:
            contract = row["comparison_contract"]
        elif row["comparison_contract"] != contract:
            raise ValueError("mixed source contracts in " + path)
        records[row["batch_id"]] = row
    return records, contract


def score_record(batch, record):
    expected = scored_pairs(batch)
    if record is None:
        return {p: "missing" for p in expected}, None
    if record.get("runner_error"):
        return {p: "crash" for p in expected}, None
    qsql = {q["qid"]: q["sql"] for q in batch["queries"]}
    qids = set(qsql)
    rows = {}
    for item in record.get("pairs") or []:
        pair = frozenset((item["a"], item["b"]))
        if len(pair) != 2 or not pair <= qids or pair in rows:
            raise ValueError("bad or duplicate pair in " + batch["batch_id"])
        rows[pair] = item
    if len(rows) != len(qids) * (len(qids) - 1) // 2:
        raise ValueError("incomplete pair graph in " + batch["batch_id"])
    dbs = record.get("dbs") or []
    errors = set(record.get("error_class") or [])
    if not errors <= qids:
        raise ValueError("unknown error query in " + batch["batch_id"])
    outcomes = {}
    checked = {}
    for item in rows.values():
        a, b = item["a"], item["b"]
        cert = item.get("certificate")
        verdict = item.get("verdict")
        if cert is None and verdict != "undecided" or cert is not None and verdict != "split":
            raise ValueError("inconsistent verdict/certificate in " + batch["batch_id"])
        if a in errors or b in errors:
            if cert is not None:
                raise ValueError("query-error pair has certificate in " + batch["batch_id"])
        elif cert is not None:
            if not isinstance(cert, int) or cert < 0 or cert >= len(dbs):
                raise ValueError("certificate index out of range in " + batch["batch_id"])
            if cert not in checked:
                checked[cert] = check_db(batch["_schema"], batch["dialect"], from_jsonable(dbs[cert]), qsql)
            if classify_from_check(checked[cert], a, b)["class"] != "genuine":
                raise ValueError("unverified pair certificate in " + batch["batch_id"])
    for a, b in expected:
        item = rows[frozenset((a, b))]
        if a in errors or b in errors:
            outcomes[a, b] = "query_error"
        elif item["certificate"] is None:
            outcomes[a, b] = "undecided"
        else:
            outcomes[a, b] = "split"
    return outcomes, {"wall": record.get("wall_secs"), "cpu": record.get("cpu_secs")
                      if record.get("cpu_complete", True) else None,
                      "cpu_complete": record.get("cpu_complete", True),
                      "cpu_lower_bound": record.get("cpu_secs"),
                      "calls": record.get("n_calls"), "dbs": len(dbs)}


def summarize(batches, run_files):
    runs = {}
    budgets = set()
    for method, seed, path in run_files:
        key = (method, seed)
        if key in runs:
            raise ValueError("duplicate method/seed")
        records, contract = load_records(path)
        if not contract or contract.get("mode") != "pair_evidence":
            raise ValueError("invalid pair contract")
        if contract.get("tool") != method or contract.get("seed") != seed:
            raise ValueError("method/seed differs from source contract")
        budgets.add(contract.get("budget_s"))
        extra = set(records) - set(batches)
        if extra:
            raise ValueError("records outside selected dataset: " + str(len(extra)))
        outcomes = {}
        costs = []
        for bid, batch in batches.items():
            scored, cost = score_record(batch, records.get(bid))
            outcomes.update({(bid, *pair): status for pair, status in scored.items()})
            if cost is not None:
                costs.append(cost)
        runs[key] = {"records": len(records), "outcomes": outcomes, "costs": costs,
                     "source": path, "contract_sha256": hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()}
    if len(budgets) != 1:
        raise ValueError("unequal batch budgets")
    known = set().union(*(set(k for k, v in run["outcomes"].items() if v == "split") for run in runs.values()))
    expected = sum(len(scored_pairs(batch)) for batch in batches.values())
    result = {"batches": len(batches), "scored_pairs": expected, "known_neq": len(known),
              "budget_s": next(iter(budgets)), "runs": {}}
    for (method, seed), run in sorted(runs.items()):
        outcomes = run["outcomes"]
        counts = collections.Counter(outcomes.values())
        missing = counts["missing"] + counts["crash"]
        caught = sum(outcomes.get(key) == "split" for key in known)
        costs = run["costs"]
        complete_costs = missing == 0 and len(costs) == len(batches)
        result["runs"][method + ":" + str(seed)] = {
            "records": run["records"], "outcomes": dict(counts), "complete": missing == 0,
            "caught": caught, "recall": (caught / len(known) if known and missing == 0 else None),
            "known_missing": sum(outcomes.get(key) in ("missing", "crash") for key in known),
            "wall_sum_s": sum(x["wall"] for x in costs) if complete_costs and all(x["wall"] is not None for x in costs) else None,
            "cpu_sum_s": sum(x["cpu"] for x in costs) if complete_costs and all(x["cpu"] is not None for x in costs) else None,
            "cpu_incomplete": sum(not x["cpu_complete"] for x in costs),
            "cpu_lower_bound_s": sum(x["cpu_lower_bound"] for x in costs)
            if complete_costs and all(x["cpu_lower_bound"] is not None for x in costs) else None,
            "calls": sum(x["calls"] for x in costs)
            if complete_costs and all(x["calls"] is not None for x in costs) else None,
            "source": run["source"],
            "contract_sha256": run["contract_sha256"]}
    by_method = collections.defaultdict(list)
    for key, row in result["runs"].items():
        method, seed = key.rsplit(":", 1)
        by_method[method].append((int(seed), row))
    result["mean_std"] = {}
    for method, rows in by_method.items():
        values = [r["recall"] for _, r in rows]
        if {seed for seed, _ in rows} != set(range(5)) or any(v is None for v in values):
            result["mean_std"][method] = None
        else:
            result["mean_std"][method] = [statistics.mean(values), statistics.stdev(values)]
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--run", action="append", required=True, help="METHOD:SEED:PATH")
    ap.add_argument("--json")
    args = ap.parse_args()
    parsed = []
    for spec in args.run:
        method, seed, path = spec.split(":", 2)
        parsed.append((method, int(seed), path))
    result = summarize(dataset(args.dataset), parsed)
    rendered = json.dumps(result, indent=2)
    if args.json:
        if os.path.exists(args.json):
            raise ValueError("refuse overwrite: " + args.json)
        with open(args.json, "w") as stream:
            stream.write(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
