"""SQLDriller hybrid checker (eval/tools/sqldriller_worker.py): stage order of Fig. 8, with the external tools
mocked so the tests run in milliseconds on any machine."""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "eval"))


@pytest.fixture(scope="module")
def sd():
    spec = importlib.util.spec_from_file_location("sqldriller_worker", os.path.join(ROOT, "eval/tools/sqldriller_worker.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # serve() only runs under __main__
    return m


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


REQ = {"q1": "SELECT 1", "q2": "SELECT 2", "timeout": 100, "seed": 0}


def run(sd, sqlsolver="NEQ", veq=None, ts=(None, 3), genuine=True, clock=None, cost=None):
    clock = clock or Clock()
    calls = {"sqlsolver": [], "verieql": [], "testsuite": []}
    cost = cost or {}

    def s(r, cap):
        calls["sqlsolver"].append(cap)
        clock.t += cost.get("sqlsolver", 1)
        return sqlsolver

    def v(r, k, cap):
        calls["verieql"].append((k, cap))
        clock.t += cost.get("verieql", 1)
        return (veq or (lambda k: {"verdict": "eq"}))(k)

    def t(r, cap):
        calls["testsuite"].append(cap)
        clock.t += cost.get("testsuite", 1)
        return ts

    out = sd.check(dict(REQ), sqlsolver=s, verieql=v, testsuite=t,
                   genuine=lambda r, i: genuine, clock=clock)
    return out, calls


def test_sqlsolver_eq_short_circuits(sd):
    out, calls = run(sd, sqlsolver="EQ")
    assert out["verdict"] == "eq" and out["stage"] == "sqlsolver_eq"
    assert calls["verieql"] == [] and calls["testsuite"] == []


@pytest.mark.parametrize("ans", ["NEQ", "UNKNOWN", "TIMEOUT"])
def test_non_eq_goes_to_verieql_ascending_and_stops_at_first_cex(sd, ans):
    veq = lambda k: {"verdict": "neq", "instance": {"t": [{"a": k}]}} if k == 3 else {"verdict": "eq"}
    out, calls = run(sd, sqlsolver=ans, veq=veq)
    assert [k for k, _ in calls["verieql"]] == [1, 2, 3]
    assert out["verdict"] == "neq" and out["stage"] == "verieql_neq@3" and out["instance"] == {"t": [{"a": 3}]}
    assert calls["testsuite"] == []


def test_all_bounds_eq_then_testsuite(sd):
    out, calls = run(sd, ts=({"t": [{"a": 9}]}, 4))
    assert [k for k, _ in calls["verieql"]] == list(range(1, 11))
    assert out["verdict"] == "neq" and out["stage"] == "testsuite_neq" and out["instance"] == {"t": [{"a": 9}]}
    assert out["testsuite_dbs"] == 4


def test_testsuite_no_difference_is_eq(sd):
    out, _ = run(sd, ts=(None, 7))
    assert out["verdict"] == "eq" and out["stage"] == "testsuite_eq" and out["testsuite_dbs"] == 7


@pytest.mark.parametrize("v", ["timeout", "unsupported", "error"])
def test_verieql_failure_falls_back_to_testsuite(sd, v):
    out, calls = run(sd, veq=lambda k: {"verdict": v} if k == 2 else {"verdict": "eq"}, ts=({"t": []}, 1))
    assert [k for k, _ in calls["verieql"]] == [1, 2]
    assert out["stage"] == "testsuite_neq"


def test_unreliable_verieql_cex_falls_back_to_testsuite(sd):
    out, calls = run(sd, veq=lambda k: {"verdict": "neq", "instance": {"t": []}}, genuine=False, ts=(None, 2))
    assert [k for k, _ in calls["verieql"]] == [1]
    assert out["verieql_bounds"] == [[1, "neq_unreliable"]] and out["stage"] == "testsuite_eq"


def test_deadline_shared_by_all_stages(sd):
    # T = 100: SQLSolver capped at 25 s; VeriEQL rounds only while > 25 s would remain for the test suite
    out, calls = run(sd, cost={"sqlsolver": 25, "verieql": 20, "testsuite": 1})
    assert calls["sqlsolver"] == [25]
    caps = [c for _, c in calls["verieql"]]
    assert caps == [50, 30, 10]          # 100-25-25, then -20 each; the 4th round would get < 1 s
    assert out["verieql_bounds"][-1] == [4, "no_budget"]
    assert calls["testsuite"] == [100 - 25 - 60]
    assert all(c <= 100 for c in caps)


def test_no_time_for_testsuite_is_timeout(sd):
    out, calls = run(sd, sqlsolver="UNKNOWN", veq=lambda k: {"verdict": "timeout"}, cost={"sqlsolver": 25, "verieql": 75})
    assert calls["testsuite"] == [] and out["verdict"] == "timeout"


def test_fuzz_sqlite_layout(sd, tmp_path):
    import sqlite3
    from seereval.schema import Schema, Table
    s = Schema("x", [Table("P", [{"name": "id", "type": "int"}, {"name": "nm", "type": "varchar"}], ["id"], [["nm"]]),
                     Table("C", [{"name": "pid", "type": "int"}], [], [],
                           [{"columns": ["pid"], "ref_table": "P", "ref_columns": ["id"]}])])
    p = str(tmp_path / "e.sqlite")
    sd._fuzz_sqlite(s, p)
    con = sqlite3.connect(p)
    assert con.execute("pragma foreign_key_list('C')").fetchall()[0][2:5] == ("P", "pid", "id")
    ddl = con.execute("select sql from sqlite_master where name='P'").fetchone()[0]
    assert "\"nm\" TEXT unique" in [l.strip().rstrip(",") for l in ddl.split("\n")]


def test_repair_composite_fk_and_int_range(sd):
    import random
    from seereval.schema import Schema, Table
    s = Schema("x", [Table("PS", [{"name": "p", "type": "int"}, {"name": "s", "type": "int"}], ["p", "s"], []),
                     Table("L", [{"name": "lp", "type": "int"}, {"name": "ls", "type": "int"}], [], [],
                           [{"columns": ["lp", "ls"], "ref_table": "PS", "ref_columns": ["p", "s"]}])])
    inst = {"ps": [{"p": 1, "s": 2}, {"p": 3, "s": 2 ** 40}], "l": [{"lp": 1, "ls": 9}, {"lp": None, "ls": None}]}
    sd._repair(s, inst, random.Random(0))
    keys = {(r["p"], r["s"]) for r in inst["ps"]}
    assert all(sd.INT_LO <= r["s"] <= sd.INT_HI for r in inst["ps"])
    assert (inst["l"][0]["lp"], inst["l"][0]["ls"]) in keys
    assert inst["l"][1] == {"lp": None, "ls": None}      # MATCH SIMPLE: an all-NULL reference stays


def test_testsuite_stage_killed_at_deadline(sd, monkeypatch):
    import time

    def slow_loop(r, cap, genuine, max_dbs, n):
        n.value = 2
        time.sleep(60)

    monkeypatch.setattr(sd, "_testsuite_loop", slow_loop)
    t = time.time()
    inst, n = sd._testsuite(dict(REQ), 1.0)
    assert inst is None and n == 2 and time.time() - t < 8


def test_testsuite_stage_error_is_error_verdict(sd, monkeypatch):
    def bad_loop(r, cap, genuine, max_dbs, n):
        raise KeyError("boom")

    monkeypatch.setattr(sd, "_testsuite_loop", bad_loop)
    with pytest.raises(RuntimeError, match="KeyError"):
        sd._testsuite(dict(REQ), 5.0)


def test_shell_sqldriller_restarts_testsuite_only(monkeypatch):
    """Restart rounds repeat only SQLDriller's randomised stage; a SQLSolver-proved pair is closed."""
    from seereval import shell
    from seereval.schema import Schema, Table
    calls = []

    def fake(tool, st, qs, timeout, seed, op="pair", bound=None, stages=None):
        calls.append((tuple(qs), stages))
        if qs == ["a", "b"]:
            return {"verdict": "eq", "stage": "sqlsolver_eq"}
        return {"verdict": "eq", "stage": "testsuite_eq"}
    monkeypatch.setattr(shell, "_tool_call", fake)
    s = Schema("s", [Table("t", [{"name": "id", "type": "int", "not_null": True}], ["id"])])
    qs = [{"qid": "a", "sql": "SELECT id FROM t"}, {"qid": "b", "sql": "SELECT id FROM t WHERE 1=1"}]
    shell.pairwise_shell("sqldriller", s, "sqlite", qs, budget_s=3, per_call_s=1, restart=True)
    assert calls == [(("a", "b"), None)]
    qs2 = [{"qid": "a", "sql": "SELECT id FROM t"}, {"qid": "c", "sql": "SELECT id FROM t WHERE id >= 0"}]
    calls.clear()
    shell.pairwise_shell("sqldriller", s, "sqlite", qs2, budget_s=1.5, per_call_s=0.1, restart=True)
    assert calls[0][1] is None and len(calls) > 1 and all(c[1] == ["testsuite"] for c in calls[1:])


def test_repair_enums_checks_and_pk(sd):
    import random
    from seereval.schema import from_ddl
    s = from_ddl("u", "create table section (sec_id int, semester varchar(6) check (semester in ('Fall', 'Spring')),"
                      " year numeric(4,0) check (year > 1701 and year < 2100), primary key (sec_id, semester));")
    inst = {"section": [{"sec_id": 1, "semester": "Fall", "year": 5}, {"sec_id": 2, "semester": "zzzzzz", "year": 2500},
                        {"sec_id": 1, "semester": "Fall", "year": 1900}]}
    sd._repair(s, inst, random.Random(0))
    rows = inst["section"]
    assert rows[0] == {"sec_id": 1, "semester": "Fall", "year": 1702}
    assert rows[1]["semester"] in ("Fall", "Spring") and rows[1]["year"] == 2099
    assert len(rows) == 2                                   # the repeated primary key (1, 'Fall') is dropped
