"""Known-answer tests for the aggregate-aware witnesses, base-row EXISTS / IN, boundary pairs and engine-evaluated
atom truth tables (tiny schemas, each a few seconds at most)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from seer2 import Refiner, Schema  # noqa: E402
from seer2.generate import AtomEval  # noqa: E402
from seer2.pools import INT  # noqa: E402

PART_LI = """CREATE TABLE part (pk INT PRIMARY KEY, brand VARCHAR(10) NOT NULL);
CREATE TABLE li (oid INT NOT NULL, ln INT NOT NULL, pk INT NOT NULL REFERENCES part(pk),
  qty DECIMAL(15,2) NOT NULL, price DECIMAL(15,2) NOT NULL, PRIMARY KEY (oid, ln));"""
DEPT_EMP = """CREATE TABLE dept (did INT PRIMARY KEY, name VARCHAR(20) NOT NULL);
CREATE TABLE emp (eid INT PRIMARY KEY, did INT REFERENCES dept(did), salary INT);"""


def classes(ddl, qs, budget=30, seed=0):
    S = Schema(ddl)
    res = Refiner(S, qs, budget=budget, seed=seed).run()
    for db in res["dbs"].values():
        assert not S.violations(db)
    return sorted(sorted(c) for c in res["classes"]), res


def test_correlated_avg_vs_global_avg():
    """Q17-style: per-part average vs the average over all parts needs two parts with different averages and a
    quantity placed between the two thresholds (multi-row, multi-group instance, Z3 arithmetic over AVG)."""
    qs = [("corr", "SELECT SUM(l.price) FROM li l JOIN part p ON p.pk = l.pk WHERE p.brand = 'b1' AND "
                   "l.qty < (SELECT 0.2 * AVG(l2.qty) FROM li l2 WHERE l2.pk = l.pk)"),
          ("glob", "SELECT SUM(l.price) FROM li l JOIN part p ON p.pk = l.pk WHERE p.brand = 'b1' AND "
                   "l.qty < (SELECT 0.2 * AVG(l2.qty) FROM li l2 WHERE l2.pk = l2.pk)")]
    cl, res = classes(PART_LI, qs)
    assert cl == [["corr"], ["glob"]], res["stop"]


def test_having_sum_threshold_edge():
    qs = [("gt", "SELECT l.pk FROM li l GROUP BY l.pk HAVING SUM(l.qty) > 30"),
          ("ge", "SELECT l.pk FROM li l GROUP BY l.pk HAVING SUM(l.qty) >= 30")]
    cl, res = classes(PART_LI, qs)
    assert cl == [["ge"], ["gt"]], res["stop"]


def test_exists_boundary_over_base_rows():
    qs = [("gt", "SELECT d.did FROM dept d WHERE EXISTS (SELECT 1 FROM emp e WHERE e.did = d.did AND e.salary > 10)"),
          ("ge", "SELECT d.did FROM dept d WHERE EXISTS (SELECT 1 FROM emp e WHERE e.did = d.did AND e.salary >= 10)"),
          ("in", "SELECT d.did FROM dept d WHERE d.did IN (SELECT e.did FROM emp e WHERE e.salary > 10)")]
    cl, res = classes(DEPT_EMP, qs)
    assert cl == [["ge"], ["gt", "in"]], res["stop"]


def test_predpair_boundary_on_plain_where():
    qs = [("gt", "SELECT eid FROM emp WHERE salary > 10"), ("ge", "SELECT eid FROM emp WHERE salary >= 10"),
          ("gt9", "SELECT eid FROM emp WHERE salary > 9")]
    cl, res = classes(DEPT_EMP, qs)
    assert cl == [["ge", "gt9"], ["gt"]]


def test_atom_truth_table_uses_target_engine():
    """Integer division: 3 / 2 = 1 in SQLite and PostgreSQL, 1.5 in DuckDB. The truth table must follow the engine
    the (same-dialect) queries run on."""
    assert AtomEval("duckdb").unary("x0 / 2 = 1", [3, 2, None], INT) == ["F", "T", "N"]
    assert AtomEval("sqlite").unary("x0 / 2 = 1", [3, 2, None], INT) == ["T", "T", "N"]


def test_atom_truth_table_postgres():
    try:
        ev = AtomEval("postgres")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no PostgreSQL: {e}")
    assert ev.unary("x0 / 2 = 1", [3, 2, None], INT) == ["T", "T", "N"]
