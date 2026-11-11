"""Batch partitioning around the baseline tools.

Contract: a batch of n queries is partitioned into classes; every split is backed by a counterexample database
that satisfies all declared constraints and on which the engine returns different results. Queries that fail on
the empty constrained schema form a separate error class and are never compared.

`pairwise_shell` (SQLDriller-style "pairwise + counterexample cache", used for SQLWitness, ParSEval, VeriEQL and
Polygon's pairwise check): queries are inserted in batch order. A query's signature is its result on every cached
database; classes always have distinct signatures. A new query whose signature matches no class opens a new
class without any tool call. Otherwise the tool is called on (query, class representative). A counterexample the
shared validator classifies as genuine is cached, every class is refined by its result on it, and the insertion
is retried; any other outcome (eq, timeout, unsupported, error, spurious, violating, load_error, exec_error)
merges the query into that class. Per-call timeout = min(per_call_s, remaining batch budget). Once the batch
budget is spent, remaining queries are placed by signature alone (`no_budget`). Every call record carries `t`,
the batch-elapsed seconds when the tool returned (time to final partition = `t` of the last genuine call).

`polygon_multi` (Polygon's native multi-query disambiguation): a worklist of classes, initially one class with
all valid queries. Each class of size >= 2 is given to `env.disambiguate`; Polygon only searches for a 2-way
split, so it is called repeatedly on unsplit classes. A returned database that the validator accepts (valid
constraints) refines every class by execution results (which can give more than 2 groups); new sub-classes go back
on the worklist. A class whose call yields no genuine split is final.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from . import tools
from .validator import check_db, classify_from_check, empty_db_errors

VALIDATOR_TIMEOUT_S = 30


def _sigval(r):
    if r is None:
        return None
    return ("ERR",) if r[0] == "ERR" else r


class _State:
    """Cached counterexample databases and every valid query's signature over them. Each new database is
    loaded once and all valid queries of the batch are run on it, so signatures never need a tool call."""

    def __init__(self, schema, dialect, sql: Dict[str, str], valid: List[str]):
        self.schema, self.dialect, self.sql, self.valid = schema, dialect, sql, valid
        self.dbs: List[Dict[str, Any]] = []
        self.sig: Dict[str, tuple] = {q: () for q in valid}

    def run_all(self, inst):
        return check_db(self.schema, self.dialect, inst, {q: self.sql[q] for q in self.valid}, VALIDATOR_TIMEOUT_S)

    def add_db(self, inst, chk):
        self.dbs.append(inst)
        for q in self.valid:
            self.sig[q] = self.sig[q] + (_sigval(chk["results"].get(q)),)


def _refine(classes: List[List[str]], st: _State) -> List[List[str]]:
    out = []
    for c in classes:
        groups: Dict[tuple, List[str]] = {}
        for q in c:
            groups.setdefault(st.sig[q], []).append(q)
        out.extend(groups.values())
    return out


def _tool_call(tool, st, qs, timeout, seed, op="pair", bound=None, stages=None):
    req = tools.build_request(tool, st.schema, st.dialect, [st.sql[q] for q in qs], timeout, seed, op=op)
    if bound is not None and tool in BOUNDED:
        req["bound"] = bound
    if stages is not None:
        req["stages"] = stages
    return tools.call(tool, req, hard_s=timeout + 45)


# restarts within the batch budget (`restart=True`): after one pass over the batch (every pair of the pass gets one
# call, as without restarts), the remaining budget goes to rounds n = 1, 2, ...: each round calls the tool again on
# every pair still in one class, with a fresh seed (randomised tools: seed + 1000 n) or the next bound (bounded tools:
# VeriEQL, Polygon's env.check, bound 2 + n), until the budget ends. So every method can spend the whole batch budget
# as SEER does, and no single pair starves the rest of the batch. A pair the tool reports outside its fragment
# (unsupported / error) is not called again: the next call would fail alike.
BOUNDED = {"verieql": 2, "polygon": 2}
NO_RESTART = {"unsupported", "error"}
# SQLDriller (SIGMOD'25, Fig. 8): SQLSolver and VeriEQL bounds 1..10 are deterministic, so a restart round repeats
# only its randomised stage, the test-suite execution, with the round's seed; a pair SQLSolver proved equivalent
# is closed (the checker returns EQ there and never reaches the later stages).
RESTART_STAGES = {"sqldriller": ["testsuite"]}
CLOSED_STAGES = {"sqlsolver_eq"}


def pairwise_shell(tool: str, schema, dialect: str, queries: List[Dict[str, str]], budget_s: float,
                   per_call_s: float = 60, seed: int = 0, restart: bool = False) -> Dict[str, Any]:
    t0 = time.time()
    sql = {q["qid"]: q["sql"] for q in queries}
    errs = empty_db_errors(schema, dialect, sql, timeout_s=VALIDATOR_TIMEOUT_S)
    valid = [x["qid"] for x in queries if x["qid"] not in errs]
    st = _State(schema, dialect, sql, valid)
    classes: List[List[str]] = []
    pairs: List[Dict[str, Any]] = []
    for q in valid:
        while True:
            sq = st.sig[q]
            match = next((c for c in classes if st.sig[c[0]] == sq), None)
            if match is None:
                classes.append([q])
                break
            rep = match[0]
            left = budget_s - (time.time() - t0)
            if left < 1:
                match.append(q)
                pairs.append({"a": rep, "b": q, "verdict": "no_budget"})
                break
            r = _tool_call(tool, st, [rep, q], min(per_call_s, left), seed, bound=BOUNDED.get(tool))
            rec = {"a": rep, "b": q, "verdict": r["verdict"], "wall": r.get("wall"), "t": round(time.time() - t0, 3),
                   "reason": (r.get("reason") or "")[:200] or None}
            for k in ("status", "iterations", "n_unsupported_constraints", "omitted_constraints", "stderr_tail", "stage",
                      "stage_secs", "verieql_bounds", "testsuite_dbs", "cpu", "cpu_complete", "host_cpu", "docker_cpu_s",
                      "upstream_commit", "native_exception_logged", "resource_contract", "resource_key", "resource_preparation_wall_s", "resource_preparation_cpu_s", "value_cache_source", "value_cache_initial_sha256", "value_cache_final_sha256", "value_cache_isolation"):
                if r.get(k) is not None:
                    rec[k] = r[k]
            if r["verdict"] == "neq" and r.get("instance") is not None:
                if tool == "sqldriller_official" and time.time() - t0 >= budget_s:
                    chk, cl = None, {"class": "over_budget"}
                else:
                    chk = st.run_all(r["instance"])
                    cl = classify_from_check(chk, rep, q)
                    if tool == "sqldriller_official" and time.time() - t0 >= budget_s:
                        cl = {"class": "over_budget"}
                rec.update({"class": cl["class"], "violations": cl.get("violations") or None,
                            "differs_unconstrained": cl.get("differs_unconstrained"),
                            "load_error": cl.get("load_error") or None})
                if cl["class"] != "genuine":
                    rec["instance"] = r["instance"]  # kept for audit; never cached
                if cl["class"] == "genuine":
                    rec["db"] = len(st.dbs)
                    st.add_db(r["instance"], chk)
                    classes = _refine(classes, st)
                    pairs.append(rec)
                    continue
            elif r["verdict"] == "neq":
                rec["class"] = "no_instance"
            pairs.append(rec)
            match.append(q)
            break
    if restart:
        _restart_rounds(tool, st, classes, pairs, t0, budget_s, per_call_s, seed)
        classes = _refine(classes, st)
    return _result(classes, errs, st, pairs, t0, budget_s)


def _restart_rounds(tool, st, classes, pairs, t0, budget_s, per_call_s, seed):
    dead = {frozenset((p["a"], p["b"])) for p in pairs if p["verdict"] in NO_RESTART or p.get("stage") in CLOSED_STAGES}
    n = 0
    while True:
        n += 1
        todo = [(c[0], q) for c in _refine(classes, st) if len(c) > 1 for q in c[1:]
                if frozenset((c[0], q)) not in dead]
        if not todo:
            return
        for rep, q in todo:
            left = budget_s - (time.time() - t0)
            if left < 1:
                return
            if st.sig[rep] != st.sig[q]:  # split earlier in this round
                continue
            kw = {"stages": RESTART_STAGES[tool]} if tool in RESTART_STAGES else {}
            r = _tool_call(tool, st, [rep, q], min(per_call_s, left), seed + 1000 * n,
                           bound=BOUNDED[tool] + n if tool in BOUNDED else None, **kw)
            rec = {"a": rep, "b": q, "verdict": r["verdict"], "wall": r.get("wall"), "t": round(time.time() - t0, 3),
                   "round": n, "reason": (r.get("reason") or "")[:200] or None}
            for key in ("cpu", "cpu_complete", "host_cpu", "docker_cpu_s", "upstream_commit", "native_exception_logged", "resource_contract", "resource_key", "resource_preparation_wall_s", "resource_preparation_cpu_s", "value_cache_source", "value_cache_initial_sha256", "value_cache_final_sha256", "value_cache_isolation", "stage"):
                if key in r:
                    rec[key] = r[key]
            if r.get("stage") in CLOSED_STAGES:
                dead.add(frozenset((rep, q)))
            if r["verdict"] in NO_RESTART:
                dead.add(frozenset((rep, q)))
            if r["verdict"] == "neq" and r.get("instance") is not None:
                if tool == "sqldriller_official" and time.time() - t0 >= budget_s:
                    chk, cl = None, {"class": "over_budget"}
                else:
                    chk = st.run_all(r["instance"])
                    cl = classify_from_check(chk, rep, q)
                    if tool == "sqldriller_official" and time.time() - t0 >= budget_s:
                        cl = {"class": "over_budget"}
                rec["class"] = cl["class"]
                if cl["class"] == "genuine":
                    rec["db"] = len(st.dbs)
                    st.add_db(r["instance"], chk)
                else:
                    rec["instance"] = r["instance"]
            pairs.append(rec)


def polygon_multi(schema, dialect: str, queries: List[Dict[str, str]], budget_s: float, per_call_s: float = 60,
                  seed: int = 0) -> Dict[str, Any]:
    t0 = time.time()
    sql = {q["qid"]: q["sql"] for q in queries}
    errs = empty_db_errors(schema, dialect, sql, timeout_s=VALIDATOR_TIMEOUT_S)
    valid = [x["qid"] for x in queries if x["qid"] not in errs]
    st = _State(schema, dialect, sql, valid)
    classes = [valid] if valid else []
    final: List[List[str]] = []
    work = list(classes)
    calls: List[Dict[str, Any]] = []
    while work:
        c = work.pop(0)
        if len(c) < 2:
            final.append(c)
            continue
        left = budget_s - (time.time() - t0)
        if left < 1:
            final.append(c)
            calls.append({"class": list(c), "verdict": "no_budget"})
            continue
        work_snapshot = list(work)
        r = _tool_call("polygon", st, c, min(per_call_s, left), seed, op="multi")
        rec = {"class_size": len(c), "members": list(c), "verdict": r["verdict"], "wall": r.get("wall"),
               "t": round(time.time() - t0, 3),
               "bound": r.get("bound"), "status": r.get("status"), "reason": (r.get("reason") or "")[:200] or None,
               "n_unsupported_constraints": r.get("n_unsupported_constraints"), "stderr_tail": r.get("stderr_tail")}
        if r["verdict"] == "neq" and r.get("instance") is not None:
            chk = st.run_all(r["instance"])
            groups = {}
            if chk["status"] == "valid":
                for q in c:
                    groups.setdefault(_sigval(chk["results"].get(q)), []).append(q)
            if chk["status"] != "valid":
                rec["outcome"] = chk["status"]  # violating / load_error
                rec["violations"] = chk["violations"][:5] or None
                rec["load_error"] = (chk.get("load_error") or "")[:200] or None
                rb = chk.get("results_bare")
                if rb:
                    rec["splits_unconstrained"] = len({_sigval(rb.get(q)) for q in c}) > 1
            elif len(groups) < 2:
                rec["outcome"] = "spurious"
            if rec.get("outcome") != "genuine" and len(groups) < 2:
                rec["instance"] = r["instance"]
            else:
                rec["outcome"] = "genuine"
                rec["groups"] = len(groups)
                rec["db"] = len(st.dbs)
                st.add_db(r["instance"], chk)
                # refine every open and final class by the new database; unchanged final classes stay final,
                # every changed class (parts of c, or of a final class the new database splits) is reopened
                calls.append(rec)
                old = list(final)
                old_final = {tuple(f) for f in old}
                work, final = [], []
                for x in _refine(work_snapshot + [c] + old, st):
                    (final if tuple(x) in old_final else work).append(x)
                continue
        calls.append(rec)
        final.append(c)
    return _result(final, errs, st, calls, t0, budget_s)


def xdatan_multi(schema, dialect: str, queries: List[Dict[str, str]], budget_s: float, per_call_s: float = 600,
                 seed: int = 0) -> Dict[str, Any]:
    """XDataN's native contract (one query and its mutants) lifted to a batch. XDataN is called with a class
    representative as its query and the other members of the class as its mutants; it generates its datasets for
    that query (as its own regression driver does) and every dataset is a candidate database. A dataset the
    validator accepts and that splits some class is cached, and every class is refined by the results on it.
    XDataN's datasets depend only on its query (and the schema), so each query is used at most once; while budget
    remains, a class that is not split is retried with its next unused member as the query (the analogue of the
    restart rounds of the pairwise shell). The run ends when every class of size >= 2 has had each member as the
    query, or the budget is spent. XDataN's own per-mutant state is kept per call: `claims` (members it reports
    Killed) and `backed` (claims that the validated datasets separate from the query). Per-call cap = remaining
    batch budget (XDataN has no per-call setting of its own)."""
    from collections import Counter
    t0 = time.time()
    sql = {q["qid"]: q["sql"] for q in queries}
    errs = empty_db_errors(schema, dialect, sql, timeout_s=VALIDATOR_TIMEOUT_S)
    valid = [x["qid"] for x in queries if x["qid"] not in errs]
    st = _State(schema, dialect, sql, valid)
    classes: List[List[str]] = [list(valid)] if valid else []
    tried: set = set()
    calls: List[Dict[str, Any]] = []
    while True:
        c = next((k for k in classes if len(k) > 1 and any(q not in tried for q in k)), None)
        if c is None:
            break
        rep = next(q for q in c if q not in tried)
        left = budget_s - (time.time() - t0)
        if left < 1:
            calls.append({"members": list(c), "rep": rep, "verdict": "no_budget"})
            break
        tried.add(rep)
        muts = [q for q in c if q != rep]
        cap = min(per_call_s, left)
        r = tools.call("xdatan", tools.build_request("xdatan", st.schema, st.dialect,
                                                     [st.sql[rep]] + [st.sql[q] for q in muts], cap, seed),
                       hard_s=cap + 45)
        states = dict(zip(muts, r.get("states") or []))
        rec = {"class_size": len(c), "members": list(c), "rep": rep, "verdict": r["verdict"], "wall": r.get("wall"),
               "reason": (r.get("reason") or "")[:200] or None, "n_datasets": r.get("n_datasets"),
               "stderr_tail": r.get("stderr_tail"), "states": states or None,
               "claims": [q for q in muts if states.get(q) == "Killed"]}
        outcomes, before = [], len(st.dbs)
        for inst in r.get("instances") or []:
            if time.time() - t0 > budget_s:      # validation is part of B for the baselines: later datasets do not count
                outcomes.append("over_budget")
                continue
            chk = st.run_all(inst)
            if chk["status"] != "valid":
                outcomes.append(chk["status"])                      # violating / load_error
                continue
            sig = {q: _sigval(chk["results"].get(q)) for q in valid}
            if any(len({sig[q] for q in k}) > 1 for k in classes):
                outcomes.append("genuine")
                st.add_db(inst, chk)
                classes = _refine(classes, st)
            else:
                outcomes.append("spurious")
        rec["t"] = round(time.time() - t0, 3)
        rec["dataset_outcomes"] = dict(Counter(outcomes)) or None
        rec["backed"] = [q for q in rec["claims"] if st.sig[q] != st.sig[rep]]
        if len(st.dbs) > before:
            rec["outcome"], rec["db"] = "genuine", list(range(before, len(st.dbs)))
        elif outcomes:
            rec["outcome"] = "spurious" if "spurious" in outcomes else outcomes[0]
        calls.append(rec)
    return _result(classes, errs, st, calls, t0, budget_s)


def _result(classes, errs, st, calls, t0, budget_s):
    from collections import Counter
    cnt = Counter()
    for p in calls:
        cnt[p.get("class") if p.get("class") and isinstance(p.get("class"), str) else p.get("outcome") or p["verdict"]] += 1
    return {"partition": classes, "error_class": sorted(errs), "error_msgs": {q: m[:160] for q, m in errs.items()},
            "n_classes": len(classes), "n_dbs": len(st.dbs), "dbs": st.dbs, "calls": calls, "counts": dict(cnt),
            "n_calls": sum(1 for p in calls if p["verdict"] != "no_budget"),
            "budget_exhausted": time.time() - t0 >= budget_s - 1, "wall_secs": round(time.time() - t0, 2)}
