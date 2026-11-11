"""The same known answers on SQLite and PostgreSQL (PostgreSQL is skipped when SEER2_PG_DSN / the server PG16 is not
reachable)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from seer2 import Refiner, Schema  # noqa: E402

DDL = """CREATE TABLE dept (did INT PRIMARY KEY, name VARCHAR(20) NOT NULL);
CREATE TABLE emp (eid INT PRIMARY KEY, did INT REFERENCES dept(did), salary INT);"""
QS = [("notin", "SELECT d.did FROM dept d WHERE d.did NOT IN (SELECT e.did FROM emp e)"),
      ("notexists", "SELECT d.did FROM dept d WHERE NOT EXISTS (SELECT 1 FROM emp e WHERE e.did = d.did)"),
      ("star", "SELECT did, COUNT(*) FROM emp GROUP BY did"),
      ("col", "SELECT did, COUNT(salary) FROM emp GROUP BY did"),
      ("gt", "SELECT eid FROM emp WHERE salary > 10"),
      ("notle", "SELECT eid FROM emp WHERE NOT (salary <= 10)")]


def _pg_ok():
    try:
        import psycopg
        psycopg.connect(os.environ.get("SEER2_PG_DSN", "host=127.0.0.1 port=55917 dbname=postgres user=probe"),
                        connect_timeout=2).close()
        return True
    except Exception:
        return False


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not _pg_ok(), reason="PostgreSQL not reachable"))])
def test_backend_known_answers(backend):
    S = Schema(DDL)
    res = Refiner(S, QS, backend=backend, budget=20, seed=0).run()
    cl = sorted(sorted(c) for c in res["classes"])
    assert ["notexists"] in cl and ["notin"] in cl
    assert ["col"] in cl and ["star"] in cl
    assert ["gt", "notle"] in cl
    assert not res["error"]
