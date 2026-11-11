from seer.stream import StreamPartition
from seer2.schema import Schema


def test_candidate_pass_never_admits_a_bounded_proof():
    calls = []

    def prover(*args):
        calls.append("sqlsolver")
        return {"status": "not_proved"}

    def quick(*args):
        calls.append("sqlwitness")
        return {"status": "no_witness", "native": {"verdict": "unknown"}}

    engine = StreamPartition(
        Schema("CREATE TABLE t(x INTEGER);", "sqlite"),
        random_dbs=0,
        budget_s=10,
        bounds=(1,),
        prover=prover,
        quick_witness=quick,
    )
    engine.add("a", "SELECT x FROM t", compare=False)
    engine.add("b", "SELECT x FROM t WHERE 1=1", compare=False)
    engine._compare("b", refutation_only=True)
    assert calls == ["sqlwitness"]
    assert engine.metrics["solver_calls"] == 0
    assert engine.relation("b")["status"] == "undecided"

    engine.refute_pending(search_budget=1.0)
    assert engine.metrics["tail_candidate_calls"] == 1
    assert engine.relation("b")["status"] == "undecided"
    assert calls == ["sqlwitness"]

    engine.retry_pending()
    assert calls == ["sqlwitness", "sqlsolver"]
    assert engine.metrics["solver_calls"] > 0
    assert engine.relation("b")["status"] == "bounded"
    assert engine.assert_invariants(replay=True)
