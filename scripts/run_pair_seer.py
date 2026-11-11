"""Isolated PairSeer runner for the shared pair-evidence candidate contract.

One record per unchanged public batch. Every raw split is checked on the
dataset engine before it receives a certificate index in the scored graph.
"""
import argparse
import hashlib
import itertools
import json
import os
import resource
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))
from seer.core import from_jsonable
from seer.pair_state import PairSeer
from seer2.schema import Schema as SeerSchema
from seereval.schema import Schema, to_ddl
from seereval.engine import normalize_sql
from seereval.validator import check_db, classify_from_check, empty_db_errors
from seer_sqlwitness import BACKEND, DIALECT, _cpu, _runq, sigma_extra


class HardTimeout(BaseException):
    pass


def _alarm(*_):
    raise HardTimeout()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def contract(args, root):
    paths = [__file__, args.dataset, ROOT / "scripts/seer_sqlwitness.py"]
    paths += sorted((ROOT / name / child).resolve() for name in ("src/seer", "src/seer2", "eval/seereval")
                    for child in os.listdir(ROOT / name) if child.endswith(".py"))
    batches = [json.loads(line) for line in open(args.dataset) if line.strip()]
    paths += sorted({Path(root, b["schema_path"]) for b in batches})
    if args.only:
        paths.append(args.only)
    return batches, {"mode": "pair_evidence", "tool": "pair_seer", "seed": args.seed,
                     "budget_s": args.budget, "symbolic_selection": args.symbolic_selection, "outcome_memo": getattr(args, "outcome_memo", False), "shared_pair_replay": getattr(args, "shared_pair_replay", False),
                     "share_query_encoding": getattr(args, "share_query_encoding", True),
                     "encode_sigma": getattr(args, "encode_sigma", True),
                     "witness_reuse":getattr(args,"witness_reuse",True),
                     "goal_policy":getattr(args,"goal_policy","all_pairs"),
                     "shard": args.shard, "files": {str(Path(p).resolve()): sha(p) for p in paths}}


def run_batch(batch, args, root):
    schema = Schema.load(os.path.join(root, batch["schema_path"]))
    dialect = batch["dialect"]
    s = SeerSchema(";\n".join(to_ddl(schema, dialect, deferrable=False)) + ";",
                   DIALECT[dialect], inclusions=sigma_extra(schema))
    queries = [(q["qid"], normalize_sql(q["sql"], dialect)) for q in batch["queries"]]
    qsql = dict(queries)
    if len(qsql) != len(queries):
        raise ValueError("duplicate query ID")
    ref = next((q["qid"] for q in batch["queries"] if q.get("role") == "reference"), queries[0][0])
    t0, cpu0, runq0 = time.time(), _cpu(), _runq()
    sr = PairSeer(s, queries, dialect=DIALECT[dialect], backend=BACKEND[DIALECT[dialect]],
                  budget=args.budget, seed=args.seed, reference=ref,
                  symbolic_selection=args.symbolic_selection, outcome_memo=getattr(args, "outcome_memo", False), shared_pair_replay=getattr(args, "shared_pair_replay", False),
                  share_query_encoding=getattr(args, "share_query_encoding", True),
                  encode_sigma=getattr(args, "encode_sigma", True), witness_reuse=getattr(args,"witness_reuse",True),goal_policy=getattr(args,"goal_policy","all_pairs"))
    result = sr.run()
    dbs = result["certificates"]["dbs"]
    errors = empty_db_errors(schema, dialect, qsql)
    checked = {}
    pairs = []
    rejected = []
    for a, b in itertools.combinations(qsql, 2):
        native = sr.verdict(a, b)
        cert = native.get("cert") if native["verdict"] == "split" else None
        reason = native.get("reason")
        if a in errors or b in errors:
            if cert is not None:
                rejected.append({"a": a, "b": b, "certificate": cert, "class": "query_error"})
            cert = None
            reason = "query_error"
        elif cert is not None:
            if cert not in checked:
                checked[cert] = check_db(schema, dialect, from_jsonable(dbs[cert]),
                    qsql if sr.witness_reuse else {q:qsql[q] for q in (a,b)})
            outcome = classify_from_check(checked[cert], a, b)["class"]
            if outcome != "genuine":
                rejected.append({"a": a, "b": b, "certificate": cert, "class": outcome})
                cert = None
                reason = "shared_validator:" + outcome
        row = {"a": a, "b": b, "certificate": cert,
               "verdict": "split" if cert is not None else "undecided"}
        if reason and cert is None:
            row["reason"] = reason
        if native["verdict"] in ("bounded_equivalent", "proved_equivalent"):
            row["native_verdict"] = native["verdict"]
            if "k" in native:
                row["k"] = native["k"]
        pairs.append(row)
    return {"state_model": "pair_evidence", "pairs": pairs, "error_class": sorted(errors),
            "error_msgs": errors, "dbs": dbs, "n_dbs": len(dbs),
            "n_genuine_pairs": sum(p["verdict"] == "split" for p in pairs),
            "rejected_claims": rejected, "late_instances": result["late_instances"],
            "stop": result["stop"], "stats": result["stats"], "timing": result["timing"],
            "symbolic_selection": args.symbolic_selection,
            "encoding_configuration": result['encoding_configuration'],
            "witness_configuration":result["witness_configuration"],
            "goal_selection_configuration":result["goal_selection_configuration"],
            "goal_selection_trace":result["goal_selection_trace"],
            "rejected_symbolic_instances": result['rejected_symbolic_instances'],
            "wall_secs": round(time.time() - t0, 3), "search_wall_s": result["wall"],
            "cpu_secs": round(_cpu() - cpu0, 3), "runq_secs": round(_runq() - runq0, 3),
            "cpu_complete": dialect == "sqlite", "n_calls": None,
            "solver_calls": result["stats"].get("solver_calls", 0),
            "db_exec": result["stats"].get("executions", 0)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--eval-root", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--budget", type=float, default=600)
    ap.add_argument("--shared-pair-replay", action="store_true")
    ap.add_argument("--goal-policy",choices=("all_pairs","safe_forest"),default="all_pairs")
    ap.add_argument("--no-cross-pair-certificate-reuse",dest="witness_reuse",action="store_false",default=True)
    ap.add_argument("--no-cross-query-memo", dest="share_query_encoding", action="store_false", default=True)
    ap.add_argument("--no-hard-sigma", dest="encode_sigma", action="store_false", default=True)
    ap.add_argument("--outcome-memo", action="store_true")
    ap.add_argument("--symbolic-selection", choices=("representatives", "max_pairs"), default="representatives")
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--only")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--mem-gb", type=float, default=8)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if Path(args.eval_root).resolve() != ROOT.resolve():
        raise ValueError("eval-root must be this isolated source tree")
    if args.mem_gb > 0 and sys.platform.startswith("linux"):
        limit = int(args.mem_gb * 2 ** 30)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    root = os.path.dirname(os.path.abspath(args.dataset))
    batches, source = contract(args, root)
    if args.sample and len(batches) > args.sample:
        step = len(batches) / args.sample
        batches = [batches[int(i * step)] for i in range(args.sample)]
    if args.only:
        keep = {line.strip() for line in open(args.only) if line.strip()}
        batches = [b for b in batches if b["batch_id"] in keep]
    i, n = map(int, args.shard.split("/"))
    batches = batches[i::n]
    done = set()
    if os.path.exists(args.out):
        for line in open(args.out):
            if not line.strip():
                continue
            prev = json.loads(line)
            if prev.get("comparison_contract") != source:
                raise ValueError("refuse mixed source contract on resume")
            if prev["batch_id"] in done:
                raise ValueError("duplicate batch record")
            done.add(prev["batch_id"])
    print(json.dumps({"start": time.strftime("%F %T"), "method": "pair_seer", "seed": args.seed,
                      "todo": len(batches) - len(done), "uptime": os.popen("uptime").read().strip()}), flush=True)
    signal.signal(signal.SIGALRM, _alarm)
    with open(args.out, "a") as stream:
        for batch in batches:
            if batch["batch_id"] in done:
                continue
            started = time.time()
            signal.alarm(int(args.budget * 3 + 120))
            try:
                result = run_batch(batch, args, root)
                error = None
            except HardTimeout:
                result = {}
                error = "hard_timeout"
            except Exception as exc:
                result = {}
                error = f"{type(exc).__name__}: {str(exc)[:300]} | {traceback.format_exc()[-600:]}"
            finally:
                signal.alarm(0)
            row = {"batch_id": batch["batch_id"], "dataset": batch["dataset"], "tool": "pair_seer",
                   "seed": args.seed, "budget": args.budget, "comparison_contract": source,
                   "state_model": "pair_evidence", **result, "runner_error": error,
                   "runner_wall": round(time.time() - started, 3)}
            stream.write(json.dumps(row, default=str) + "\n")
            stream.flush()
            print(json.dumps({"batch_id": batch["batch_id"], "genuine": row.get("n_genuine_pairs"),
                              "runner_error": error, "wall": row["runner_wall"]}), flush=True)
    print(json.dumps({"end": time.strftime("%F %T"), "uptime": os.popen("uptime").read().strip()}), flush=True)


if __name__ == "__main__":
    main()
