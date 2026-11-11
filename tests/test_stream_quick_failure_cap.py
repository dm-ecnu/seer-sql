from seer.stream import StreamPartition
from seer2.schema import Schema


def test_repeated_empty_quick_calls_leave_members_undecided():
    calls = []

    def no_witness(*args):
        calls.append(1)
        return {"status": "no_witness", "native": {"verdict": "unknown"}}

    engine = StreamPartition(
        Schema("CREATE TABLE t(x INTEGER);", "sqlite"),
        random_dbs=0,
        budget_s=10,
        quick_witness=no_witness,
        quick_failure_cap=3,
    )
    engine.add("r", "SELECT x FROM t", compare=False)
    for i in range(4):
        q = f"q{i}"
        engine.add(q, "SELECT x FROM t", compare=False)
        engine._compare(q, refutation_only=True)
        assert engine.relation(q)["status"] == "undecided"
    assert len(calls) == engine.metrics["quick_witness_calls"] == 3
    assert engine.metrics["quick_witness_skipped_after_failures"] == 1
    assert engine.assert_invariants(replay=True)
