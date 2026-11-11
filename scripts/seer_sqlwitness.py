"""End-to-end run of SEER (full system) or the seer2 v2 prototype on the SQLWitness-derived batch datasets, with
every split re-checked by the shared counterexample validator of the evaluation harness (seereval.validator,
the one every baseline goes through: full declared constraints, the dataset's own engine).

  python scripts/seer_sqlwitness.py --eval-root EVAL --dataset DATA/spider_pairs.jsonl --method seer|v2 \
      --budget 120 --seed 0 --shard 0/11 --out OUT.jsonl

Batch format: {"batch_id","dataset","dialect","schema_path","queries":[{"qid","sql","role"}],"meta"}; the query
with role "reference" is the gold query. Execution engine of SEER / v2 per dialect: sqlite -> SQLite,
postgresql -> PostgreSQL (SEER_PG_URL), mysql -> MySQL (SEER_MYSQL_URL, same sql_mode as the validator); the
validator always uses the dataset's own engine.

Queries are passed in the dataset's convention (seereval.engine.normalize_sql, as SQLWitness and the validator
run them). One output line per batch: per candidate the verdict (split / bounded_equivalent / undecided / query_error) and,
for splits, the validator class of the certificate (genuine / spurious / violating / load_error / exec_error).
Resumes: batches already in OUT are skipped.
"""
import argparse
import hashlib
import subprocess
import json
import os
import resource
import signal
import sys
import time
import traceback

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
os.environ.setdefault("MALLOC_ARENA_MAX", "2")

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
from seer2.schema import Schema                  # noqa: E402

DIALECT = {"postgresql": "postgres", "sqlite": "sqlite", "mysql": "mysql", "duckdb": "duckdb"}
BACKEND = {"postgres": "postgres", "sqlite": "sqlite", "mysql": "mysql", "duckdb": "duckdb"}


class HardTimeout(Exception):
    pass


def _alarm(*_):
    raise HardTimeout()


def sigma_extra(es):
    """Constraints of the dataset's Σ that its DDL leaves out (the validator checks them in Python): inclusion
    dependencies to non-key columns and FKs between columns of different declared types. SEER and v2 get them as
    undeclared inclusions, so they generate, encode and check the same Σ the validator applies."""
    out = [(i["table"], i["columns"], i["ref_table"], i["ref_columns"]) for i in es.inclusions]
    for t in es.tables:
        for fk in t.foreign_keys:
            if es.is_key(fk["ref_table"], fk["ref_columns"]):
                pt = es.table(fk["ref_table"])
                if any(t.col(c)["type"] != pt.col(pc)["type"] for c, pc in zip(fk["columns"], fk["ref_columns"])):
                    out.append((t.name, fk["columns"], fk["ref_table"], fk["ref_columns"]))
    return out


def _cpu():
    """CPU seconds (user + sys) of this process and its reaped children; one batch runs at a time per process."""
    s, c = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime


def _runq():
    """Run-queue delay (s) summed over this process's live threads; 0 where /proc is unavailable."""
    tot = 0.0
    try:
        for t in os.listdir("/proc/self/task"):
            try:
                tot += int(open(f"/proc/self/task/{t}/schedstat").read().split()[1]) / 1e9
            except (OSError, ValueError, IndexError):
                pass
    except OSError:
        pass
    return tot


def run_batch(b, a, root, cache):
    from seereval.schema import Schema as ESchema, to_ddl
    from seereval.validator import check_db, classify_from_check
    from seereval.engine import normalize_sql
    from seer.core import Seer, from_jsonable
    from seer2.refine import Refiner
    d = DIALECT[b["dialect"]]
    key = b["schema_path"]
    if key not in cache:
        es = ESchema.load(os.path.join(root, key))
        cache[key] = (es, Schema(";\n".join(to_ddl(es, b["dialect"], deferrable=False)) + ";", d,
                                 inclusions=sigma_extra(es)))
    es, S = cache[key]
    # the dataset's query convention (SQLWitness lowercases outside string literals, PostgreSQL quoted identifiers
    # included; the evaluation's engines run every tool's queries that way): SEER and v2 get the queries as executed
    qs = [(q["qid"], normalize_sql(q["sql"], b["dialect"])) for q in b["queries"]]
    ref = next((q["qid"] for q in b["queries"] if q.get("role") == "reference"), qs[0][0])
    cands = [q for q, _ in qs if q != ref]
    sql = dict(qs)
    t0, c0, q0 = time.time(), _cpu(), _runq()
    per, certs = {}, {}
    if len(qs) < 2:
        return {"wall": 0.0, "stop": "trivial", "pairs": {}}
    if a.method == "seer":
        sr = Seer(S, qs, dialect=d, backend=BACKEND[d], budget=a.budget, seed=a.seed, reference=ref,
                  symbolic_selection=getattr(a, "symbolic_selection", "representatives"))
        res = sr.run()
        for c in cands:
            v = sr.verdict(ref, c)
            per[c] = {"verdict": v["verdict"], **({"reason": v["reason"]} if "reason" in v else {}),
                      **({"k": v["k"]} if "k" in v else {})}
            if v["verdict"] == "split":
                certs.setdefault(v["cert"], []).append(c)
        dbs = {i: from_jsonable(db) for i, db in enumerate(res["certificates"]["dbs"])}
        claimed = sorted({e[0] for e in res["certificates"]["events"]})
        extra = {"stop": res["stop"], "partition_s": res.get("partition_s"), "timing": res["timing"], "stats": res["stats"],
                 "unsupported": {q: v[0] for q, v in res["unsupported"].items()},
                 "symbolic_selection": res["symbolic_selection"],
                 "symbolic_selection_trace": res["symbolic_selection_trace"]}
    else:
        rf = Refiner(S, qs, backend=BACKEND[d], dialect=d, budget=a.budget, seed=a.seed, reference=ref)
        res = rf.run()
        for c in cands:
            v = res["verdicts"][c]
            vv = {"NEQ": "split", "EQ_BOUNDED": "bounded_equivalent", "ERROR": "query_error"}.get(v["verdict"],
                                                                                                  "undecided")
            per[c] = {"verdict": vv}
            if vv == "split":
                certs.setdefault(v["db"], []).append(c)
        dbs = rf.dbs
        claimed = list(range(len(dbs)))
        extra = {"stop": res["stop"], "n_db": res["n_db"], "n_executed": res["n_executed"]}
    wall, cpu, runq = time.time() - t0, _cpu() - c0, _runq() - q0
    from seer.core import _jsonable as to_jsonable
    kept = [{"db": to_jsonable(dbs[i]), "idx": i, "cands": certs.get(i, [])} for i in claimed] if a.keep_dbs else None
    # independent validation of every certificate on the dataset's engine (full declared constraints)
    for i, cs in certs.items():
        try:
            chk = check_db(es, b["dialect"], dbs[i], {q: sql[q] for q in [ref] + cs}, 30)
            for c in cs:
                cl = classify_from_check(chk, ref, c)
                per[c]["cls"] = cl["class"]
        except Exception as e:
            for c in cs:
                per[c]["cls"] = f"validator_error:{type(e).__name__}"
    # the batch partition as the evaluation shell builds it for every baseline: queries failing on the empty
    # constrained schema form the error class; the others are refined by their results on each claimed
    # certificate database the validator accepts (status valid). `vpartition` is what the matrix scores.
    from seereval.shell import _State, _refine
    from seereval.validator import empty_db_errors
    errs = empty_db_errors(es, b["dialect"], sql, timeout_s=30)
    valid = [q for q, _ in qs if q not in errs]
    st = _State(es, b["dialect"], sql, valid)
    n_valid_dbs = 0
    for i in claimed:
        try:
            chk = st.run_all(dbs[i])
        except Exception:
            continue
        if chk["status"] == "valid":
            st.add_db(dbs[i], chk)
            n_valid_dbs += 1
    vpart = _refine([valid] if valid else [], st)
    return {"wall": round(wall, 3), "cpu": round(cpu, 3), "runq": round(runq, 3), "pairs": per, "vpartition": vpart, "error_class": sorted(errs),
            "n_claimed_dbs": len(claimed), "n_valid_dbs": n_valid_dbs, **extra, **({"certs": kept} if kept is not None else {})}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-root", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--method", required=True, choices=["seer", "v2"])
    ap.add_argument("--budget", type=float, default=120)
    ap.add_argument("--symbolic-selection", choices=["representatives", "max_pairs"], default="representatives")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--mem-gb", type=float, default=8)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", type=int, default=0, help="N evenly spaced batches, the same as run_baseline.py --sample")
    ap.add_argument("--only", help="file with one batch_id per line (re-runs of a subset)")
    ap.add_argument("--no-keep-dbs", dest="keep_dbs", action="store_false",
                    help="do not store the certificate databases (stored by default, for offline re-validation)")
    a = ap.parse_args()
    sys.setrecursionlimit(4000)          # deeply nested LeetCode queries; deeper ones become `too_deep`
    sys.path.insert(0, os.path.join(a.eval_root, "eval"))
    if a.mem_gb > 0 and sys.platform.startswith("linux"):
        lim = int(a.mem_gb * 2 ** 30)
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
    i, n = map(int, a.shard.split("/"))
    root = os.path.dirname(os.path.abspath(a.dataset))
    provenance = {"code_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip(),
                  "dataset_sha256": hashlib.sha256(open(a.dataset, "rb").read()).hexdigest(),
                  "selection_ids_sha256": hashlib.sha256(open(a.only, "rb").read()).hexdigest() if a.only else None,
                  "symbolic_selection": a.symbolic_selection,
                  "source_sha256": {rel: hashlib.sha256(open(os.path.join(ROOT, rel), "rb").read()).hexdigest()
                                    for rel in ["scripts/seer_sqlwitness.py", "src/seer/core.py", "src/seer/symbolic.py", "src/seer/encode.py"]}}

    batches = [json.loads(l) for l in open(a.dataset)]
    if a.sample and len(batches) > a.sample:
        step = len(batches) / a.sample
        batches = [batches[int(j * step)] for j in range(a.sample)]
    if a.only:
        keep = {l.strip() for l in open(a.only) if l.strip()}
        batches = [b for b in batches if b["batch_id"] in keep]
    batches = batches[i::n]
    done = set()
    if os.path.exists(a.out):
        done = {json.loads(l)["batch_id"] for l in open(a.out) if l.strip()}
    cache = {}
    signal.signal(signal.SIGALRM, _alarm)
    with open(a.out, "a") as f:
        for b in batches:
            schema_path = os.path.join(root, b["schema_path"])
            schema_sha = hashlib.sha256(open(schema_path, "rb").read()).hexdigest()
            if b["batch_id"] in done:
                continue
            t, c = time.time(), _cpu()
            signal.alarm(int(a.budget * 3 + 120))
            try:
                rec = run_batch(b, a, root, cache)
                err = None
            except HardTimeout:
                rec, err = {}, "hard_timeout"
            except Exception as e:
                rec, err = {}, f"{type(e).__name__}: {str(e)[:300]} | {traceback.format_exc()[-600:]}"
            finally:
                signal.alarm(0)
            out = {"batch_id": b["batch_id"], "dataset": b["dataset"], "method": a.method, "seed": a.seed,
                   "budget": a.budget, "n_queries": len(b["queries"]), "meta": b.get("meta"), **rec,
                   "provenance": {**provenance, "schema_sha256": schema_sha},
                   "runner_error": err, "runner_wall": round(time.time() - t, 2),
                   "cpu_secs": round(_cpu() - c, 3)}
            f.write(json.dumps(out, default=str) + "\n")
            f.flush()


if __name__ == "__main__":
    main()
