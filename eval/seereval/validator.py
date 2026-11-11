"""Shared counterexample validator. Every counterexample from every tool goes through `check_db`:

  1. constraint check: the instance is loaded into the dialect's engine with every declarable constraint in the DDL
     (PK, UNIQUE, NOT NULL, single and composite FK, CHECK), and the remaining constraints (FK/inclusion to non-key
     columns, enums, domains, and a second pass over PK/FK/NOT NULL with MATCH SIMPLE) are checked in Python;
  2. execution: every query of interest is run on the engine over that instance; results are compared with bag
     semantics, or list semantics when both queries have a top-level ORDER BY.

Pair classification (`classify_pair`):
  genuine     constraint-valid instance on which the engine returns different results
  spurious    constraint-valid instance on which the engine returns the same results
  violating   the instance breaks a declared constraint (`differs_unconstrained` tells whether it would at least
              separate the queries on a constraint-free copy of the schema)
  load_error  the engine rejects the instance for a non-constraint reason (e.g. a value of the wrong type)
  exec_error  a query fails on the engine over this instance
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .engine import ExecError, LoadError, make_engine, python_violations, result_key, has_top_order_by

CONSTRAINT_ERR = ("IntegrityError", "ForeignKeyViolation", "UniqueViolation", "NotNullViolation", "CheckViolation",
                  "constraint", "FOREIGN KEY", "UNIQUE", "NOT NULL", "Duplicate entry", "cannot be null",
                  "foreign key", "Check constraint")


class QueryInfo:
    def __init__(self, sql: str, dialect: str):
        self.sql = sql
        self.ordered = has_top_order_by(sql, dialect)


def check_db(schema, dialect: str, instance: Dict[str, List[Dict[str, Any]]], queries: Dict[str, str],
             timeout_s: float = 30, also_bare: bool = True) -> Dict[str, Any]:
    """Load `instance` with constraints and run every query. Returns
    {"status": valid|violating|load_error, "violations": [...], "load_error": str|None,
     "results": {qid: result_key | ("ERR", msg)}, "results_bare": {...} (only when not valid and also_bare)}"""
    pv = python_violations(schema, instance)
    out: Dict[str, Any] = {"violations": pv, "load_error": None, "results": {}}
    eng = make_engine(dialect, schema, timeout_s)
    try:
        try:
            eng.load(instance)
            loaded = True
        except LoadError as e:
            loaded = False
            msg = str(e)
            out["load_error"] = msg
            if any(k.lower() in msg.lower() for k in CONSTRAINT_ERR):
                pv.append("engine: " + msg[:200])
        if pv:
            out["status"] = "violating"
        elif not loaded:
            out["status"] = "load_error"
        else:
            out["status"] = "valid"
        if loaded and not pv:
            out["results"] = _run_all(eng, queries, dialect)
    finally:
        eng.close()
    if out["status"] != "valid" and also_bare:
        b = make_engine(dialect, schema, timeout_s, bare=True)
        try:
            try:
                b.load(instance)
                out["results_bare"] = _run_all(b, queries, dialect)
            except LoadError as e:
                out["results_bare"] = None
                out["bare_load_error"] = str(e)[:200]
        finally:
            b.close()
    return out


def _run_all(eng, queries, dialect):
    res = {}
    for qid, sql in queries.items():
        try:
            rows = eng.run(sql)
            res[qid] = result_key(rows, has_top_order_by(sql, dialect))
        except ExecError as e:
            res[qid] = ("ERR", str(e)[:200])
    return res


def differs(r1, r2) -> Optional[bool]:
    """None if either errored."""
    if r1 is None or r2 is None or r1[0] == "ERR" or r2[0] == "ERR":
        return None
    if r1[0] == "L" and r2[0] == "L":
        return r1 != r2
    # one side ordered, the other not: compare as bags
    return _bag(r1) != _bag(r2)


def _bag(r):
    if r[0] == "B":
        return r[1]
    from collections import Counter
    return tuple(sorted(Counter(r[1]).items(), key=lambda kv: repr(kv)))


def classify_pair(schema, dialect, instance, sql1, sql2, timeout_s=30) -> Dict[str, Any]:
    chk = check_db(schema, dialect, instance, {"a": sql1, "b": sql2}, timeout_s)
    return classify_from_check(chk, "a", "b")


def classify_from_check(chk, a, b) -> Dict[str, Any]:
    out = {"status": chk["status"], "violations": chk["violations"][:5]}
    if chk["status"] == "valid":
        d = differs(chk["results"].get(a), chk["results"].get(b))
        out["class"] = {True: "genuine", False: "spurious", None: "exec_error"}[d]
    elif chk["status"] == "violating":
        out["class"] = "violating"
    else:
        out["class"] = "load_error"
        out["load_error"] = (chk.get("load_error") or "")[:200]
    rb = chk.get("results_bare")
    if chk["status"] != "valid" and rb:
        out["differs_unconstrained"] = differs(rb.get(a), rb.get(b))
    return out


def empty_db_errors(schema, dialect, queries: Dict[str, str], timeout_s=30) -> Dict[str, str]:
    """Queries that fail on the engine over the empty constrained schema (syntax/semantic errors). The contract
    puts them in a separate error class; they are never counted as 'distinguished'."""
    chk = check_db(schema, dialect, {}, queries, timeout_s, also_bare=False)
    return {q: r[1] for q, r in chk["results"].items() if r[0] == "ERR"}
