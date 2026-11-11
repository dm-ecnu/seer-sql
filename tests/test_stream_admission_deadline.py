import time

from seer.stream import Edge, StreamPartition
from seer.stream_runner import run_sqlwitness
from seer2.schema import Schema


def make_engine():
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')
    engine = StreamPartition(schema, 'sqlite', 'sqlite', budget_s=10, random_dbs=0)
    engine.add('a', 'SELECT x FROM t', compare=False)
    engine.add('b', 'SELECT x FROM t', compare=False)
    return engine


def test_overrunning_search_cannot_admit_bounded_edge(monkeypatch):
    engine = make_engine()

    def late_search(*_args, **_kwargs):
        time.sleep(0.03)
        return {'status': 'unsupported', 'bounded_k': 4, 'levels': [],
                'domain_sha256': engine.search.domain_identity()}

    monkeypatch.setattr(engine.search, 'search', late_search)
    engine.deadline = time.perf_counter() + 0.01
    engine._compare('b', rep='a', force=True)
    assert engine.edges == []
    assert engine.metrics['late_evidence_rejected'] == 1
    assert engine.pending_reason['b'][1] == 'pair_or_batch_deadline'


def test_replay_finishing_after_deadline_cannot_enter_pool(monkeypatch):
    engine = make_engine()
    original_run = engine._run

    def slow_run(*args, **kwargs):
        time.sleep(0.03)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(engine, '_run', slow_run)
    before = len(engine.witnesses)
    engine.deadline = time.perf_counter() + 0.01
    assert engine._install({'t': [{'x': 1}]}) is False
    assert len(engine.witnesses) == before
    assert engine.metrics['late_evidence_rejected'] == 1


def test_pool_index_update_crossing_deadline_rolls_back(monkeypatch):
    engine = make_engine()
    original_event = engine._coverage_event

    def slow_event(*args, **kwargs):
        original_event(*args, **kwargs)
        time.sleep(0.03)

    monkeypatch.setattr(engine, '_coverage_event', slow_event)
    before = (len(engine.witnesses), dict(engine.fingerprints),
              dict(engine.representatives), len(engine.events))
    engine.deadline = time.perf_counter() + 0.01
    assert engine._install({'t': [{'x': 1}]}) is False
    assert (len(engine.witnesses), engine.fingerprints,
            engine.representatives) == before[:3]
    assert not any(e.get('event') == 'pool_added' for e in engine.events[before[3]:])


def test_edge_update_crossing_deadline_rolls_back(monkeypatch):
    engine = make_engine()
    engine.random_representative = True
    original_choice = engine._rep_rng.choice

    def slow_choice(*args, **kwargs):
        time.sleep(0.03)
        return original_choice(*args, **kwargs)

    monkeypatch.setattr(engine._rep_rng, 'choice', slow_choice)
    engine.deadline = time.perf_counter() + 0.01
    edge = Edge('a', 'b', 'bounded', {}, 4, engine.search.domain_identity())
    assert engine._add_edge(edge) is False
    assert engine.edges == []
    assert engine.adjacency == {}
    assert engine.metrics['late_evidence_rejected'] == 1


def test_late_pair_tool_database_cannot_enter_pool(monkeypatch):
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')

    def slow_tool(*_args, **_kwargs):
        time.sleep(0.08)
        return {'native': {'verdict': 'neq'}, 'database': {'t': [{'x': 1}]}}

    monkeypatch.setattr('seer.stream_runner.native_sqlwitness', slow_tool)
    engine, result = run_sqlwitness(
        schema, None, [('a', 'SELECT x FROM t'),
                       ('b', 'SELECT x FROM t WHERE x=1')],
        'sqlite', 'sqlite', 0.05, 0, random_dbs=0)
    assert result['metrics']['late_evidence_rejected'] >= 1
    assert len(engine.witnesses) == 1
    assert result['pair_counts']['separated'] == 0


def test_encoding_finishing_after_deadline_rolls_back_arrival(monkeypatch):
    engine = make_engine()
    original_add = engine.search.add_query
    before = (list(engine.arrival), engine.search.domain_identity(),
              engine.search.domain_inputs.relation_roots_seen, len(engine.events))

    def slow_add(*args, **kwargs):
        original_add(*args, **kwargs)
        time.sleep(0.03)

    monkeypatch.setattr(engine.search, 'add_query', slow_add)
    engine.deadline = time.perf_counter() + 0.01
    assert engine.add('c', "SELECT x FROM t WHERE x = 17") is False
    assert engine.arrival == before[0]
    assert engine.search.domain_identity() == before[1]
    assert engine.search.domain_inputs.relation_roots_seen == before[2]
    assert 'c' not in engine.queries and 'c' not in engine.search.queries
    assert not any(e.get('event') == 'arrival' for e in engine.events[before[3]:])
    assert engine.metrics['late_arrivals_rejected'] == 1
    engine.assert_invariants(replay=False)


def test_pool_execution_finishing_after_deadline_rolls_back_arrival(monkeypatch):
    engine = make_engine()
    original_run = engine._run
    before = (list(engine.arrival), dict(engine.fingerprints), len(engine.events))

    def slow_run(*args, **kwargs):
        time.sleep(0.03)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(engine, '_run', slow_run)
    engine.deadline = time.perf_counter() + 0.01
    assert engine.add('c', "SELECT x FROM t WHERE x = 17") is False
    assert engine.arrival == before[0] and engine.fingerprints == before[1]
    assert all('c' not in w.observations for w in engine.witnesses)
    assert not any(e.get('event') == 'arrival' for e in engine.events[before[2]:])
    assert engine.metrics['late_arrivals_rejected'] == 1
    engine.assert_invariants(replay=False)
