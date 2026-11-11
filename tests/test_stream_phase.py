from seer.stream import StreamPartition
from seer.stream_runner import run_sqlwitness
from seer2.schema import Schema


def test_phase_counters_follow_actual_encoding_pool_and_replay():
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')
    engine = StreamPartition(schema, 'sqlite', 'sqlite', budget_s=10, random_dbs=0)
    engine.add('a', 'SELECT x FROM t')
    engine.add('b', 'SELECT x FROM t WHERE x=1')
    engine.assert_invariants(replay=True)
    metrics = engine.snapshot()['metrics']
    assert metrics['encoding_s'] > 0
    assert metrics['pool_execution_s'] > 0
    assert metrics['replay_s'] > 0
    assert all(metrics[k] >= 0 for k in ('refutation_search_s', 'bounded_proof_s'))
    assert sum(metrics[k] for k in ('encoding_s', 'pool_execution_s',
               'refutation_search_s', 'bounded_proof_s', 'replay_s')) <= metrics['wall_s']


def test_native_pair_call_is_charged_to_refutation_search(monkeypatch):
    from seer import stream_runner
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')
    def absent(*args, **kwargs):
        return {'status': 'no_witness', 'native': {'verdict': 'unknown'}, 'database': None}
    monkeypatch.setattr(stream_runner, 'native_sqlwitness', absent)
    _, result = run_sqlwitness(schema, None,
        [('a', 'SELECT x FROM t'), ('b', 'SELECT x FROM t')],
        'sqlite', 'sqlite', 0.1, 0, random_dbs=0)
    # The frozen opponent shell retries unresolved pairs in new rounds.
    assert result['metrics']['native_pair_calls'] >= 1
    assert result['metrics']['refutation_search_s'] > 0
