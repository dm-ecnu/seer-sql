import itertools
import sys
import time
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seer.pair_state import PairSeer
from seer.core import replay
from seer2.schema import Schema
from seer2.refine import Out

DDL = "CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);"
QUERIES = [("bridge", "SELECT x FROM t"),
           ("ascending", "SELECT x FROM t ORDER BY x ASC"),
           ("descending", "SELECT x FROM t ORDER BY x DESC")]


def engine(queries=QUERIES, **kwargs):
    return PairSeer(Schema(DDL, "sqlite"), queries, dialect="sqlite", backend="sqlite",
                    fast=False, budget=10, levels=(1, 2), max_k=2, **kwargs)


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
@pytest.mark.parametrize("policy", ["representatives", "max_pairs"])
def test_actual_sql_mixed_order_bridge_all_permutations(order, policy):
    seer = engine([QUERIES[i] for i in order], symbolic_selection=policy)
    result = seer.run()
    assert seer.verdict("ascending", "descending")["verdict"] == "split", result
    assert seer.verdict("bridge", "ascending") == {"verdict": "bounded_equivalent", "k": 2}
    assert seer.verdict("bridge", "descending") == {"verdict": "bounded_equivalent", "k": 2}
    assert result["residual_pair_count"] == 2
    assert result["residual_components"] == [["ascending", "bridge", "descending"]]
    assert result["stats"].get("model_mismatch", 0) == 0
    assert "classes" not in result
    assert replay(seer.S, result, dialect="sqlite", backend="sqlite") == 1


def test_cross_type_bag_difference_and_execution_is_shared():
    seer = engine(QUERIES + [("none", "SELECT x FROM t WHERE 0")])
    before = seer.stats["executions"]
    assert seer.run_db({"t": [{"x": 1}, {"x": 2}]}, "spec") == 4
    assert seer.stats["executions"] - before == 4
    seer.recheck()
    for name, _ in QUERIES:
        assert seer.verdict(name, "none")["verdict"] == "split"
    assert seer.verdict("bridge", "ascending")["verdict"] == "undecided"


def test_real_database_finishing_after_budget_is_retained_without_credit():
    seer = engine(QUERIES[1:])
    original = seer._exec_all
    def slow(inst, db, qs):
        time.sleep(0.02)
        return original(inst, db, qs)
    seer._exec_all = slow
    seer.budget = seer.elapsed() + 0.01
    db = {"t": [{"x": 1}, {"x": 2}]}
    assert seer.run_db(db, "late_spec") == 0
    assert seer.verdict("ascending", "descending")["verdict"] == "undecided"
    assert seer.result()["late_instances"][0]["db"] == db
    assert seer.result()["stats"]["over_budget_db"] == 1


def test_same_order_queries_are_proved_only_as_pairs():
    seer = engine([(name, "SELECT x FROM t ORDER BY x") for name in ("a", "b", "c")])
    result = seer.run()
    assert result["stop"] == "bounded_equivalent"
    assert len(result["proved_pairs"]) == 3
    assert all(v == {"verdict": "bounded_equivalent", "k": 2} for v in result["verdicts"].values())


def test_per_instance_error_does_not_block_unrelated_pair():
    seer = engine(QUERIES + [("fault", "SELECT x FROM t")])
    original = seer._exec_all
    def execute(inst, db, qs):
        outs = original(inst, db, qs)
        if "fault" in outs:
            outs["fault"] = Out("err", msg="injected per-instance timeout")
        return outs
    seer._exec_all = execute
    assert seer.run_db({"t": [{"x": 1}, {"x": 2}]}, "spec") == 1
    assert seer.verdict("ascending", "descending")["verdict"] == "split"
    assert seer.verdict("fault", "ascending")["verdict"] == "undecided"


def test_replay_failure_reopens_pair_and_withdraws_split():
    seer = engine(QUERIES[1:])
    assert seer.run_db({"t": [{"x": 1}, {"x": 2}]}, "spec") == 1
    original = seer._exec_all
    def changed(inst, db, qs):
        outs = original(inst, db, qs)
        outs["descending"] = outs["ascending"]
        return outs
    seer._exec_all = changed
    seer.recheck()
    assert seer.unsplit_pairs() == 1
    assert seer.verdict("ascending", "descending")["verdict"] == "undecided"
    assert seer.stop != "all_split"
    assert seer.result()["certificates"]["events"] == []


def test_incremental_query_compares_to_all_old_queries():
    seer = engine(QUERIES[:2])
    seer.run()
    seer.add_query(*QUERIES[2])
    assert seer.verdict("ascending", "descending")["verdict"] == "split"
    assert seer.verdict("bridge", "descending") == {"verdict": "bounded_equivalent", "k": 2}


def test_actual_sql_homogeneous_count_witnesses():
    queries = [("all", "SELECT COUNT(*) FROM t"), ("positive", "SELECT COUNT(*) FROM t WHERE x > 0"),
               ("distinct", "SELECT COUNT(DISTINCT x) FROM t")]
    seer = engine(queries)
    result = seer.run()
    assert seer.verdict("all", "positive")["verdict"] == "split"
    assert seer.verdict("all", "distinct") == {"verdict": "bounded_equivalent", "k": 2}
    assert replay(seer.S, result, dialect="sqlite", backend="sqlite") >= 1


def test_sqlite_import_does_not_create_unused_duckdb_default_connection():
    import subprocess
    program = "import sys; import seer.pair_state; assert \"duckdb\" not in sys.modules"
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_pair_graph_oom_retains_certified_edge_and_keeps_other_edges_unknown():
    import z3
    seer = engine()
    assert seer.run_db({"t": [{"x": 1}, {"x": 2}]}, "spec") == 1
    def exhausted():
        raise z3.Z3Exception("out of memory")
    seer.symbolic_tier = exhausted
    result = seer.run()
    assert result["stop"] == "resource_limit"
    assert seer.verdict("ascending", "descending")["verdict"] == "split"
    for q in ("ascending", "descending"):
        assert seer.verdict("bridge", q) == {"verdict": "undecided", "reason": "solver_out_of_memory"}
    assert replay(seer.S, result, dialect="sqlite", backend="sqlite") == 1


def test_invalid_query_stays_unknown_without_blocking_live_pair():
    seer = engine(QUERIES[1:] + [("invalid", "SELECT missing FROM t")])
    result = seer.run()
    assert seer.verdict("ascending", "descending")["verdict"] == "split"
    assert seer.verdict("invalid", "ascending") == {"verdict": "undecided", "reason": "query_error"}
    assert "invalid" in result["query_error"]
