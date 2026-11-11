"""Running example for the paper: four candidate queries, one shared sequence of constraint-valid instances.

  q_ref   : departments without employees, NOT EXISTS (reference)
  q_notin : NOT IN over a nullable FK column (differs only when emp.did is NULL)
  q_left  : LEFT JOIN ... IS NULL anti-join (equivalent to q_ref)
  q_cnt   : GROUP BY / HAVING COUNT(e.eid) = 0 (equivalent to q_ref)
  q_star  : HAVING COUNT(*) = 0 (never true for a LEFT JOIN group: differs as soon as a department has no employee)

Usage: .venv/bin/python examples/seer2_running_example.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from seer2 import Refiner, Schema  # noqa: E402

DDL = """CREATE TABLE dept (did INT PRIMARY KEY, name VARCHAR(20) NOT NULL);
CREATE TABLE emp (eid INT PRIMARY KEY, did INT REFERENCES dept(did), salary INT CHECK (salary >= 0));"""
Q = [("q_ref", "SELECT d.did FROM dept d WHERE NOT EXISTS (SELECT 1 FROM emp e WHERE e.did = d.did)"),
     ("q_notin", "SELECT d.did FROM dept d WHERE d.did NOT IN (SELECT e.did FROM emp e)"),
     ("q_left", "SELECT d.did FROM dept d LEFT JOIN emp e ON e.did = d.did WHERE e.eid IS NULL"),
     ("q_cnt", "SELECT d.did FROM dept d LEFT JOIN emp e ON e.did = d.did GROUP BY d.did HAVING COUNT(e.eid) = 0"),
     ("q_star", "SELECT d.did FROM dept d LEFT JOIN emp e ON e.did = d.did GROUP BY d.did HAVING COUNT(*) = 0")]

if __name__ == "__main__":
    S = Schema(DDL)
    print("schema order:", S.order, "| emp.did nullable:", S.column("emp", "did").nullable)
    res = Refiner(S, Q, budget=20, seed=0, reference="q_ref", trace=print).run()
    print("\nstop:", res["stop"], "| certificate DBs:", res["n_db"], "| executed instances:", res["n_executed"],
          "| wall", res["wall"], "s")
    print("final classes:", res["classes"])
    for sp in res["splits"]:
        print(f"split on DB#{sp['db']}: {sp['parent']} -> {sp['blocks']}")
        print("   instance:", res["dbs"][sp["db"]])
    print("verdicts vs q_ref:", {k: v["verdict"] for k, v in res["verdicts"].items()})
    print("targets:", res["targets"])
