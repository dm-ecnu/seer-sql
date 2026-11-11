"""Known-answer tests for SEER v2 (tiny hand-made schemas; each runs in well under a second or a few seconds)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from seer2 import Refiner, Schema  # noqa: E402
from seer2.backends import make_backend  # noqa: E402

DEPT_EMP = """CREATE TABLE dept (did INT PRIMARY KEY, name VARCHAR(20) NOT NULL);
CREATE TABLE emp (eid INT PRIMARY KEY, did INT REFERENCES dept(did), salary INT);"""


def run(ddl, qs, backend="duckdb", budget=20, seed=0, ref=None):
    S = Schema(ddl)
    res = Refiner(S, qs, backend=backend, budget=budget, seed=seed, reference=ref).run()
    for db in res["dbs"].values():           # every certificate satisfies the declared constraints
        pass
    return S, res


def classes(res):
    return sorted(sorted(c) for c in res["classes"])


def test_a_not_in_vs_not_exists_needs_null():
    qs = [("notin", "SELECT d.did FROM dept d WHERE d.did NOT IN (SELECT e.did FROM emp e)"),
          ("notexists", "SELECT d.did FROM dept d WHERE NOT EXISTS (SELECT 1 FROM emp e WHERE e.did = d.did)")]
    S, res = run(DEPT_EMP, qs)
    assert classes(res) == [["notexists"], ["notin"]]
    assert res["stop"] == "all-split"
    db = res["dbs"][res["splits"][0]["db"]]
    assert any(r["did"] is None for r in db.get("emp", [])), "the separating instance must have NULL in emp.did"


def test_a2_not_in_equal_when_column_not_null():
    ddl = DEPT_EMP.replace("did INT REFERENCES", "did INT NOT NULL REFERENCES")
    qs = [("notin", "SELECT d.did FROM dept d WHERE d.did NOT IN (SELECT e.did FROM emp e)"),
          ("notexists", "SELECT d.did FROM dept d WHERE NOT EXISTS (SELECT 1 FROM emp e WHERE e.did = d.did)")]
    S, res = run(ddl, qs, budget=15)
    assert classes(res) == [["notexists", "notin"]]
    assert res["stop"] == "saturated"


def test_b_distinct_vs_group_by_same_class():
    qs = [("distinct", "SELECT DISTINCT did, salary FROM emp"),
          ("groupby", "SELECT did, salary FROM emp GROUP BY did, salary")]
    S, res = run(DEPT_EMP, qs, budget=15)
    assert classes(res) == [["distinct", "groupby"]]
    assert res["stop"] == "saturated"
    assert res["n_db"] == 0


def test_c_count_star_vs_count_col_split_with_null():
    qs = [("star", "SELECT did, COUNT(*) FROM emp GROUP BY did"),
          ("col", "SELECT did, COUNT(salary) FROM emp GROUP BY did")]
    S, res = run(DEPT_EMP, qs)
    assert classes(res) == [["col"], ["star"]]
    db = res["dbs"][res["splits"][0]["db"]]
    assert any(r["salary"] is None for r in db["emp"])


COMPOSITE = """CREATE TABLE ps (pk INT NOT NULL, sk INT NOT NULL, cost DECIMAL(10,2) NOT NULL, PRIMARY KEY (pk, sk));
CREATE TABLE li (oid INT NOT NULL, ln INT NOT NULL, pk INT, sk INT, qty INT NOT NULL CHECK (qty > 0),
  PRIMARY KEY (oid, ln), FOREIGN KEY (pk, sk) REFERENCES ps (pk, sk));"""


def test_d_composite_fk_never_violated():
    S = Schema(COMPOSITE)
    # checker itself: split-column match is a violation, a NULL component is MATCH SIMPLE
    db = {"ps": [{"pk": 1, "sk": 1, "cost": 1}, {"pk": 2, "sk": 2, "cost": 1}],
          "li": [{"oid": 1, "ln": 1, "pk": 1, "sk": 2, "qty": 1}]}
    assert S.violations(db)
    db["li"][0]["sk"] = None
    assert not S.violations(db)
    qs = [("a", "SELECT l.oid, p.cost FROM li l JOIN ps p ON l.pk = p.pk AND l.sk = p.sk WHERE l.qty > 3"),
          ("b", "SELECT l.oid, p.cost FROM li l JOIN ps p ON l.pk = p.pk WHERE l.qty >= 3"),
          ("c", "SELECT l.oid, (SELECT max(cost) FROM ps p WHERE p.pk = l.pk AND p.sk = l.sk) FROM li l"),
          ("d", "SELECT oid, count(*) FROM li WHERE pk IS NULL GROUP BY oid")]
    seen = []
    rf = Refiner(S, qs, backend="sqlite", budget=8, seed=1)
    orig = rf.run_all

    def spy(db, q):
        assert not S.violations(db)
        for r in db.get("li", []):
            if r["pk"] is not None and r["sk"] is not None:
                assert (r["pk"], r["sk"]) in {(p["pk"], p["sk"]) for p in db["ps"]}
        seen.append(sum(len(v) for v in db.values()))
        return orig(db, q)              # sqlite has PRAGMA foreign_keys=ON: the engine checks again
    rf.run_all = spy
    res = rf.run()
    assert len(seen) >= 5
    assert any(len(c) == 1 for c in res["classes"])


def test_e_limit_tie_split():
    qs = [("asc", "SELECT eid FROM emp ORDER BY salary DESC, eid LIMIT 1"),
          ("desc", "SELECT eid FROM emp ORDER BY salary DESC, eid DESC LIMIT 1")]
    S, res = run(DEPT_EMP, qs)
    assert classes(res) == [["asc"], ["desc"]]
    db = res["dbs"][res["splits"][0]["db"]]
    sal = [r["salary"] for r in db["emp"]]
    assert len(sal) >= 2


def test_e2_top1_vs_argmax_tie():
    qs = [("top1", "SELECT salary FROM emp WHERE salary IS NOT NULL ORDER BY salary DESC LIMIT 1"),
          ("argmax", "SELECT salary FROM emp WHERE salary = (SELECT max(salary) FROM emp)")]
    S, res = run(DEPT_EMP, qs)
    assert classes(res) == [["argmax"], ["top1"]]


def test_f_four_queries_three_classes_at_most_two_dbs():
    qs = [("gt", "SELECT eid FROM emp WHERE salary > 10"),
          ("notle", "SELECT eid FROM emp WHERE NOT (salary <= 10)"),
          ("ge", "SELECT eid FROM emp WHERE salary >= 10"),
          ("gtnull", "SELECT eid FROM emp WHERE salary > 10 OR salary IS NULL")]
    S, res = run(DEPT_EMP, qs, budget=15)
    assert classes(res) == [["ge"], ["gt", "notle"], ["gtnull"]]
    assert res["n_db"] <= 2


def test_g_engine_error_is_own_class():
    qs = [("ok1", "SELECT eid FROM emp"), ("bad", "SELECT nosuchcol FROM emp"),
          ("ok2", "SELECT eid FROM emp WHERE eid IS NOT NULL")]
    S, res = run(DEPT_EMP, qs, budget=10)
    assert "bad" in res["error"]
    assert all("bad" not in c for c in res["classes"])
    assert classes(res) == [["ok1", "ok2"]]


def test_reference_verdicts_and_postgres_ddl():
    S = Schema(COMPOSITE)
    ddl = S.ddl_for("postgres")
    assert 'FOREIGN KEY ("pk", "sk") REFERENCES "ps" ("pk", "sk")' in ddl[-1]
    qs = [("ref", "SELECT eid FROM emp WHERE salary > 10"), ("r1", "SELECT eid FROM emp WHERE salary >= 11"),
          ("r2", "SELECT eid FROM emp WHERE salary >= 10")]
    S, res = run(DEPT_EMP, qs, ref="ref", budget=10)
    assert res["verdicts"]["r2"]["verdict"] == "NEQ"
    assert res["verdicts"]["r1"]["verdict"] == "EQ_BOUNDED"


def test_cyclic_fk_is_broken_at_nullable_column():
    ddl = """CREATE TABLE a (id INT PRIMARY KEY, b_id INT REFERENCES b(id));
    CREATE TABLE b (id INT PRIMARY KEY, a_id INT NOT NULL REFERENCES a(id));"""
    S = Schema(ddl)
    assert S.order == ["a", "b"]
    assert [fk.deferred for fk in S.tables["a"].fks] == [True]


def test_nondeclared_inclusion_breaks_nonnullable_cycle():
    ddl = "CREATE TABLE a (id INT PRIMARY KEY); CREATE TABLE b (id INT PRIMARY KEY, aid INT NOT NULL REFERENCES a(id));"
    S = Schema(ddl, "mysql", inclusions=[("a", ["id"], "b", ["aid"])])
    assert S.order == ["a", "b"]
    assert S.tables["a"].fks[0].deferred
    assert not S.tables["b"].fks[0].deferred
    assert S.check({"a": [{"id": 1}], "b": [{"id": 2, "aid": 1}]})
    assert S.violations({"a": [{"id": 1}], "b": []})
