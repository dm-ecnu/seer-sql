"""Score the opponent matrix: SEER, the v2 ablation and the baselines of eval/run_baseline.py on one dataset.

  python scripts/matrix_score.py --dataset DATA/leetcode_batches.jsonl --runs RUN_DIR [--tag leetcode_batches] \
      [--labels runs/labels.jsonl] [--json OUT]

Run files are RUN_DIR/{tag}.{method}.s{seed}[.{shard}].jsonl; SEER and v2 come from scripts/seer_sqlwitness.py,
the baselines (sqlwitness, parseval, verieql, polygon_pair, polygon) from eval/run_baseline.py.

Pairs. With a reference query (TPC-H, TPC-DS, SQLWitness pairs, LeetCode) the scored pairs are (reference,
candidate); without one (Cubes) every pair of queries with different SQL text. A pair is *separated* by a method
when both queries are outside the error class (they run on the empty constrained schema) and fall in different
classes of the method's final partition. Every method's partition is built the same way: classes refined by the
query results on each certificate database the shared validator accepts (eval/seereval/shell.py for the
baselines; `vpartition` of seer_sqlwitness.py for SEER and v2), so every separated pair is backed by a database
that satisfies all declared constraints and on which the dataset's engine returns different results.

Known-inequivalent set: the union, over all methods and seeds present, of separated pairs (plus, with --labels,
the TPC-H pairs labelled neq by the bounded tester). Recall = separated ∩ known / known, per seed, then mean ± std.

Per method and seed also: invalid certificates (databases offered as counterexamples that the validator rejects:
violating, load_error, or spurious = valid but not separating), the verified-certificate rate (valid separating /
offered), eq on known (known-inequivalent pairs the method closed as eq: a bounded-equivalence claim for SEER,
Polygon and VeriEQL, "search ended without a counterexample" for SQLWitness, ParSEval and v2), the decidable subset
(pairs with a definite verdict: separated, eq, or an invalid certificate), time to final partition and wall time
per batch (median, p90; time to final partition = time of the method's last separation, over the batches that hold
a known-inequivalent pair, censored at the batch wall time when the method separates nothing there), and calls (SEER: z3 checks and DB executions; v2: DB executions; baselines: tool calls).
"""
import argparse
import collections
import glob
import hashlib
import itertools
import json
import os
import re
import statistics

BASELINES = ("sqlwitness", "parseval", "verieql", "polygon_pair", "polygon", "xdatan", "sqldriller")


def q(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def ms(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return (round(statistics.mean(xs), 4), round(statistics.stdev(xs), 4) if len(xs) > 1 else 0.0)


DEV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runs", "dev_ids")


def is_dev(dataset, batch_id):
    """Development split (protocol, 2026-10-21): the batches SEER was diagnosed on during the aborted first wave
    (runs/dev_ids/<dataset>.txt: Spider 111, BIRD 160 pairs), plus a fixed held-out tenth of every dataset
    (sha1(batch_id) % 10 == 0) for any later diagnosis. Dev batches are never scored in the test tables.
    TPC-H (22 query batches, the opponents' published benchmark) is scored whole and has no dev part: no method
    change may be diagnosed from it."""
    if dataset in NO_DEV:
        return False
    f = os.path.join(DEV_DIR, f"{dataset}.txt")
    if f not in _DEV:
        _DEV[f] = {l.strip() for l in open(f) if l.strip()} if os.path.exists(f) else set()
    return batch_id in _DEV[f] or int(hashlib.sha1(batch_id.encode()).hexdigest(), 16) % 10 == 0


_DEV = {}
# scored whole (no dev part): TPC-H, and XDataN's two home benchmarks (added 2026-10-22 with SEER frozen; no
# method change may be diagnosed from them)
NO_DEV = {"tpch", "xdatabm", "tpch_mut"}


def load_dataset(path, sample=0, split="test"):
    bs = [json.loads(l) for l in open(path) if l.strip()]
    if sample and len(bs) > sample:
        step = len(bs) / sample
        bs = [bs[int(i * step)] for i in range(sample)]
    name = os.path.basename(path)[:-6]
    if split != "all":
        bs = [b for b in bs if is_dev(name, b["batch_id"]) == (split == "dev")]
    out = {}
    for b in bs:
        ref = next((x["qid"] for x in b["queries"] if x.get("role") == "reference"), None)
        sql = {x["qid"]: x["sql"] for x in b["queries"]}
        ids = [x["qid"] for x in b["queries"]]
        if ref is not None:
            prs = [(ref, c) for c in ids if c != ref]
        else:
            prs = [(a, c) for a, c in itertools.combinations(ids, 2) if sql[a].strip() != sql[c].strip()]
        out[b["batch_id"]] = {"ref": ref, "ids": ids, "pairs": prs,
                              "author_neq": {(ref, x["qid"]) for x in b["queries"] if x.get("author_label") == "neq"}}
    return out


def records(pats):
    recs = {}
    for f in sorted(f for p in ([pats] if isinstance(pats, str) else pats) for f in glob.glob(p)):
        for l in open(f):
            if l.strip():
                r = json.loads(l)
                if r.get("state_model") in ("pair_evidence", "residual_pair_graph"):
                    raise ValueError("pair-evidence output needs its own scorer; refuse legacy partition inference")
                old = recs.get(r["batch_id"])
                if old is None or (old.get("runner_error") and not r.get("runner_error")):
                    recs[r["batch_id"]] = r
    return recs


def cls_of(part):
    return {x: i for i, c in enumerate(part or []) for x in c}


def outcome_runner(r, a, c, cmap, errs):
    """SEER / v2 record: per-pair verdicts are against the reference only."""
    v = (r.get("pairs") or {}).get(c) if a == (r.get("_ref")) else None
    if v is None:
        return "undecided"
    if v["verdict"] == "bounded_equivalent":
        return "eq"
    if v["verdict"] == "split":
        return "invalid_cert"           # claimed split that the validator-refined partition does not carry
    return "undecided"


def outcome_shell(r, a, c):
    calls = r.get("calls") or []
    if r.get("tool") == "xdatan":   # a Killed claim the validated datasets do not back is an invalid certificate
        for k in calls:
            if (k.get("rep") == a and c in (k.get("claims") or []) and c not in (k.get("backed") or [])) or \
                    (k.get("rep") == c and a in (k.get("claims") or []) and a not in (k.get("backed") or [])):
                return "invalid_cert"
        return "undecided"
    if r.get("tool") == "polygon":
        last = None
        for k in calls:
            if a in (k.get("members") or []) and c in (k.get("members") or []):
                last = k
        if last is None:
            return "undecided"
        if last["verdict"] == "eq":
            return "eq"
        if last["verdict"] == "neq" and last.get("outcome") in ("spurious", "violating", "load_error", "exec_error"):
            return "invalid_cert"
        return "undecided"
    mine = [k for k in calls if k.get("b") == c] or [k for k in calls if k.get("b") == a]
    if not mine:
        return "undecided"
    last = mine[-1]
    if last["verdict"] == "eq":
        return "eq"
    if last["verdict"] == "neq" and last.get("class") not in (None, "genuine", "no_instance"):
        return "invalid_cert"
    return "undecided"


def cert_counts(r, method):
    """(offered, valid separating) certificate databases."""
    if method in ("seer", "v2"):
        # certificates offered for (reference, candidate) splits; one is verified when the validator classifies
        # every pair it was offered for as genuine
        cs = [x for x in (r.get("certs") or []) if x.get("cands")]
        if r.get("certs") is None:
            return None, None
        ok = sum(1 for x in cs if all((r["pairs"].get(c) or {}).get("cls") == "genuine" for c in x["cands"]))
        return len(cs), ok
    calls = r.get("calls") or []
    if r.get("tool") == "xdatan":   # certificates = XDataN's Killed claims; valid = backed by a validated dataset
        return (sum(len(k.get("claims") or []) for k in calls), sum(len(k.get("backed") or []) for k in calls))
    if r.get("tool") == "polygon":
        off = [k for k in calls if k["verdict"] == "neq"]
        return len(off), sum(1 for k in off if k.get("outcome") == "genuine")
    off = [k for k in calls if k["verdict"] == "neq" and k.get("class") not in (None, "no_instance")]
    return len(off), sum(1 for k in off if k.get("class") == "genuine")


def calls_of(r, method):
    if method == "seer":
        st = r.get("stats") or {}
        return {"z3_checks": st.get("solver_calls", 0), "db_exec": st.get("executions", 0)}
    if method == "v2":
        return {"db_exec": r.get("n_executed", 0)}
    return {"tool_calls": r.get("n_calls", 0)}


def times_of(r, method):
    if method == "seer":
        return r.get("partition_s"), r.get("wall")
    if method == "v2":
        return r.get("wall"), r.get("wall")     # v2 reports no split timestamps: time to partition = wall
    ts = [k.get("t") for k in (r.get("calls") or []) if
          (k.get("class") == "genuine" or k.get("outcome") == "genuine") and k.get("t") is not None]
    return (max(ts) if ts else 0.0), r.get("wall_secs")


INFRA_RE = re.compile(r"QueryCanceled|OperationalError|InterfaceError|could not connect|server closed the connection|"
                      r"too many clients|connection to server|terminating connection")


def is_infra(err) -> bool:
    """A batch lost to the shared validator/database server (2026-10-22 incident: the wave-1 validator PostgreSQL
    overloaded), not to the method: scored as `infra`, kept out of that method's recall denominator, and rerun
    after ALL_DONE (scripts/infra_rerun.py); a rerun record replaces it (records() prefers error-free records)."""
    return bool(err) and bool(INFRA_RE.search(err))


TIMEOUT_V = {"timeout", "no_budget"}


def why_undecided(r, method, a, c):
    """Reason a pair stayed undecided: unsupported (outside the method's fragment), timeout (solver / call / budget
    ran out, or the pair was never reached), or error (the tool failed on it)."""
    if method in ("seer", "v2"):
        if c in (r.get("unsupported") or {}) or a in (r.get("unsupported") or {}):
            return "unsupported"
        v = (r.get("pairs") or {}).get(c) or {}
        why = str(v.get("reason") or "")
        if why.startswith("unsupported") or "not encodable" in why:
            return "unsupported"
        return "timeout"
    last = None
    for k in r.get("calls") or []:
        if {k.get("a"), k.get("b")} == {a, c} or (a in (k.get("members") or ()) and c in (k.get("members") or ())):
            last = k
    if last is None:
        return "timeout"
    v = last.get("verdict")
    if v == "unsupported":
        return "unsupported"
    if v in TIMEOUT_V:
        return "timeout"
    if v == "error":
        return "error"
    return "timeout" if v not in ("eq", "neq") else "other"


def score_run(ds, recs, method):
    """Per pair outcome: sep / eq / invalid_cert / undecided:{unsupported,timeout,error,other} / query_error /
    crash / infra."""
    out, per_batch = {}, []
    for bid, B in ds.items():
        r = recs.get(bid)
        if r is None or r.get("runner_error"):
            o = "infra" if r is not None and is_infra(r.get("runner_error")) else "crash"
            for a, c in B["pairs"]:
                out[(bid, a, c)] = o
            continue
        r["_ref"] = B["ref"] if B["ref"] is not None else B["ids"][0]
        part = r.get("vpartition") if method in ("seer", "v2") else r.get("partition")
        if part is None and method in ("seer", "v2") and B["ref"] is not None:   # older runner output
            part = None
        cmap = cls_of(part)
        errs = set(r.get("error_class") or [])
        for a, c in B["pairs"]:
            if a in errs or c in errs:
                o = "query_error"
            elif a in cmap and c in cmap and cmap[a] != cmap[c]:
                o = "sep"
            elif method in ("seer", "v2"):
                o = outcome_runner(r, a, c, cmap, errs)
            else:
                o = outcome_shell(r, a, c)
            if o == "undecided":
                o = "undecided:" + why_undecided(r, method, a, c)
            out[(bid, a, c)] = o
        tp, tw = times_of(r, method)
        cpu = r.get("cpu") if method in ("seer", "v2") else r.get("cpu_secs")
        cpu_lower_bound = cpu
        cpu_complete = r.get("cpu_complete", True)
        if not cpu_complete:
            cpu = None
        bud = r.get("budget")
        rq = r.get("runq") if method in ("seer", "v2") else r.get("runq_secs")
        # budget expired mainly through contention: the batch ran to its budget and its processes spent more than
        # half of that wall time runnable but waiting for a CPU (run-queue delay). wall/CPU alone is no test here:
        # time inside the database server (PostgreSQL, MySQL) is not CPU of the method's own processes.
        cont = bool(rq is not None and tw and bud and tw >= 0.95 * bud and rq > 0.5 * tw)
        per_batch.append({"bid": bid, "t_part": tp, "wall": tw, "cpu": cpu, "cpu_complete": cpu_complete, "cpu_lower_bound": cpu_lower_bound, "runq": rq, "contended": cont, "certs": cert_counts(r, method), "calls": calls_of(r, method),
                          "n": len(B["ids"])})
    return out, per_batch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--runs", required=True, help="run directory, or several comma-separated (waves 1 and 2)")
    ap.add_argument("--tag")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--labels", help="TPC-H runs/labels.jsonl (neq labels join the known set)")
    ap.add_argument("--json")
    ap.add_argument("--split", default="test", choices=["test", "dev", "all"],
                    help="test (default): scored tables; dev: the development split, the only one to diagnose on")
    ap.add_argument("--drop", action="append", default=[], metavar="DIR:M1,M2",
                    help="ignore these methods' files in that run directory (superseded runs, e.g. SEER at d3c3696)")
    a = ap.parse_args()
    tag = a.tag or os.path.basename(a.dataset)[:-6]
    drop = {}
    for x in a.drop:
        d, dm = x.rsplit(":", 1)
        drop.setdefault(os.path.normpath(d), set()).update(dm.split(","))
    ds = load_dataset(a.dataset, a.sample, a.split)
    runs = {}
    dirs = a.runs.split(",")
    for f in [f for d in dirs for f in glob.glob(os.path.join(d, f"{tag}.*.s*.jsonl"))]:
        m = re.match(re.escape(tag) + r"\.([a-z_0-9]+)\.s(\d+)(?:\.\d+)?\.jsonl$", os.path.basename(f))
        if m and m.group(1) not in drop.get(os.path.normpath(os.path.dirname(f)), ()):
            runs.setdefault((m.group(1), int(m.group(2))), None)
    res = {}
    for (method, seed) in sorted(runs):
        recs = records([os.path.join(d, f"{tag}.{method}.s{seed}*.jsonl") for d in dirs
                        if method not in drop.get(os.path.normpath(d), ())])
        recs = {k: v for k, v in recs.items() if k in ds}
        res[method, seed] = (len(recs),) + score_run(ds, recs, method)
    known = {k for (_, _), (_, o, _) in res.items() for k, v in o.items() if v == "sep"}
    lab_known = set()
    if a.labels:
        for l in open(a.labels):
            r = json.loads(l)
            if r["label"] == "neq":
                k = (f"tpch-q{r['qid']:02d}", "orig", f"r{r['rid']}")
                if k[0] in ds:
                    lab_known.add(k)
        known |= lab_known
    # XDataN's home benchmarks: every mutant is labelled non-equivalent by the benchmark authors; reported apart
    # from the known-inequivalent union (author_caught / author_recall), as XDataN reports its kill rate
    author = {(bid,) + p for bid, B in ds.items() for p in B["author_neq"]}
    out = {"dataset": tag, "batches": len(ds), "pairs": sum(len(B["pairs"]) for B in ds.values()),
           "author_neq": len(author) or None,
           "known_neq": len(known), "labels_neq": len(lab_known) or None, "methods": {}}
    by_m = collections.defaultdict(list)
    for (method, seed), v in sorted(res.items()):
        by_m[method].append((seed, v))
    seer_by_seed = {s: v[1] for s, v in by_m.get("seer", [])}
    for method, lst in by_m.items():
        rows = []
        for seed, (nrec, o, pb) in lst:
            cnt = collections.Counter(o.values())
            sep = {k for k, v in o.items() if v == "sep"}
            dec = {k for k, v in o.items() if v in ("sep", "eq", "invalid_cert")}
            off = sum(p["certs"][0] or 0 for p in pb if p["certs"][0] is not None)
            ok = sum(p["certs"][1] or 0 for p in pb if p["certs"][1] is not None)
            cal = collections.Counter()
            for p in pb:
                cal.update(p["calls"])
            kb = {k[0] for k in known}
            sb = {k[0] for k in sep}
            tparts = [(p["t_part"] if p["bid"] in sb else p["wall"]) for p in pb if p["bid"] in kb]
            infra = {k for k, v in o.items() if v == "infra"}
            kn = known - infra          # infrastructure failures are not counted against the method (missing)
            row = {"seed": seed, "records": nrec, "outcomes": dict(cnt), "recall": len(sep & kn) / max(1, len(kn)),
                   "infra_batches": len({k[0] for k in infra}), "infra_known_pairs": len(known & infra),
                   "caught": len(sep & known), "invalid_certs": cnt.get("invalid_cert", 0),
                   "certs_offered": off, "cert_valid_rate": (ok / off) if off else None,
                   "eq_on_known": sum(1 for k in known if o.get(k) == "eq"),
                   "decidable": len(dec), "known_in_decidable": len(dec & known),
                   "recall_on_decidable": len(sep & known & dec) / max(1, len(dec & known)),
                   "t_part_med": q(tparts, .5), "t_part_p90": q(tparts, .9),
                   "wall_med": q([p["wall"] for p in pb], .5), "wall_p90": q([p["wall"] for p in pb], .9),
                   "wall_sum_h": round(sum(p["wall"] or 0 for p in pb) / 3600, 3),
                   "cpu_med": q([p["cpu"] for p in pb if p["cpu"] is not None], .5),
                   "cpu_p90": q([p["cpu"] for p in pb if p["cpu"] is not None], .9),
                   "cpu_sum_h": round(sum(p["cpu"] or 0 for p in pb) / 3600, 3),
                   "cpu_incomplete": sum(not p["cpu_complete"] for p in pb),
                   "cpu_sum_lower_bound_h": round(sum(p["cpu_lower_bound"] or 0 for p in pb) / 3600, 3),
                   "cpu_missing": sum(1 for p in pb if p["cpu"] is None),
                   "runq_sum_h": round(sum(p["runq"] or 0 for p in pb) / 3600, 3),
                   "wall_over_cpu_med": q([p["wall"] / p["cpu"] for p in pb if p["cpu"] and p["wall"]], .5),
                   "contended_batches": sum(p["contended"] for p in pb),
                   "contended_ids": [p["bid"] for p in pb if p["contended"]][:50],
                   "calls": {k: v for k, v in cal.items()}}
            if method != "seer" and seed in seer_by_seed:
                so = seer_by_seed[seed]
                row["seer_recall_on_this_decidable"] = (len({k for k in dec & known if so.get(k) == "sep"})
                                                        / max(1, len(dec & known)))
            if lab_known:
                row["labels_caught"] = len(sep & lab_known)
            if author:
                row["author_caught"] = len(sep & author)
                row["author_recall"] = len(sep & author) / len(author)
            rows.append(row)
        agg = {k: ms([r[k] for r in rows]) for k in ("recall", "caught", "invalid_certs", "cert_valid_rate", "eq_on_known",
                                                      "decidable", "recall_on_decidable", "seer_recall_on_this_decidable",
                                                      "t_part_med", "t_part_p90", "wall_med", "wall_p90",
                                                      "cpu_med", "cpu_p90", "contended_batches", "infra_batches",
                                                      "labels_caught", "author_caught", "author_recall")
               if any(k in r for r in rows)}
        out["methods"][method] = {"seeds": [r["seed"] for r in rows], "mean_std": agg, "per_seed": rows}
    # still-missed known pairs of SEER (any seed)
    if "seer" in by_m:
        miss = collections.Counter()
        for seed, (_, o, _) in by_m["seer"]:
            for k in known:
                if o.get(k) != "sep":
                    miss[(k, o.get(k))] += 1
        out["seer_missed"] = [{"pair": list(k), "outcome": oc, "seeds": n} for (k, oc), n in miss.most_common(60)]
    print(f"== {tag}: {out['batches']} batches, {out['pairs']} pairs, known neq {out['known_neq']}"
          + (f" (labels {len(lab_known)})" if lab_known else ""))
    for method, d in out["methods"].items():
        m = d["mean_std"]
        f = lambda k: ("-" if not m.get(k) else f"{m[k][0]:.3f}±{m[k][1]:.3f}")
        print(f"  {method:13s} seeds {d['seeds']}  recall {f('recall')}  invalid {f('invalid_certs')}  "
              f"cert_ok {f('cert_valid_rate')}  eq_on_known {f('eq_on_known')}  dec {f('decidable')} "
              f"rec@dec {f('recall_on_decidable')} seer@dec {f('seer_recall_on_this_decidable')}  "
              f"t_part med {f('t_part_med')} p90 {f('t_part_p90')}  cpu med {f('cpu_med')} p90 {f('cpu_p90')}  "
              f"wall med {f('wall_med')} p90 {f('wall_p90')}  contended {f('contended_batches')}  "
              f"infra {f('infra_batches')}"
              + (f"  author_recall {f('author_recall')}" if author else ""))
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
