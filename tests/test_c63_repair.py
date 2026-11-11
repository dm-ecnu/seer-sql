import pickle
import time
from pathlib import Path
import pytest
from seer.representative_search import RepresentativeSearch
from seer.stream import StreamPartition, Edge, Observation
from seer.stream_numeric import bounded_numeric_admission
from seer2.schema import Schema


def test_numeric_ineligibility_must_not_suppress_refutation():
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')
    engine = StreamPartition(schema, random_dbs=0, budget_s=20, bounds=(1,))
    engine.add('a', 'SELECT AVG(x) FROM t', compare=False)
    engine.add('b', 'SELECT AVG(x) + 1 FROM t', compare=False)
    preflight = bounded_numeric_admission(engine.search.irs['a'], engine.search.irs['b'],
                                          'sqlite', schema)
    assert preflight == {'admitted': False, 'reason': 'exact_avg_dialect_not_supported'}
    engine._compare('b', rep='a')
    assert engine.fingerprints['a'] != engine.fingerprints['b']
    assert engine.assert_invariants()


def test_deep_hard_timeout_preserves_shallow_workers(monkeypatch):
    original = RepresentativeSearch._level
    def level(self, k, deadline):
        if k >= 4:
            time.sleep(10)
        return original(self, k, deadline)
    monkeypatch.setattr(RepresentativeSearch, '_level', level)
    search = RepresentativeSearch(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
                                  'sqlite', persistent_worker=True)
    search.add_query('a', 'SELECT x FROM t')
    search.add_query('b', 'SELECT x FROM t')
    try:
        first = search.search('a', 'b', 2, canonical_contract=True)
        assert first['bounded_k'] == 2
        assert first['query_sha256'] and first['representative_sha256']
        assert first['schema_sha256'] and first['scope']
        assert 'k' not in first
        assert first['reason'] == 'hard_timeout_after_bounded_progress'
        pids = {k: state[0].pid for k, state in search._level_workers.items()}
        second = search.search('a', 'b', 2, bounds=(1, 2), canonical_contract=True)
        assert second['bounded_k'] == 2 and second['status'] == 'domain_unsat'
        assert all(row['encoding_reused'] for row in second['levels'])
        assert pids == {k: state[0].pid for k, state in search._level_workers.items()}
    finally:
        search.close_worker()
    assert not search._level_workers


@pytest.mark.parametrize('batch', [False, True])
def test_inherited_result_uses_actual_member_and_preserves_context(tmp_path, batch):
    engine = StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
                             random_dbs=0, budget_s=20)
    engine.evidence_dir = str(tmp_path)
    engine.add('a', 'SELECT COUNT(*) FROM t', compare=False)
    engine.add('b', 'SELECT COUNT(x) FROM t', compare=False)
    # Inject the erroneous edge that the old inheritance path trusted.
    edge = Edge('a', 'b', 'bounded', {}, 2, engine.search.domain_identity())
    engine.edges.append(edge)
    engine.adjacency = {'a': [edge], 'b': [edge]}
    database = {'t': [{'x': None}]}
    admitted = engine.install_batch([(database, ('a', 'b'))]) if batch else engine._install(database, target=('a', 'b'))
    assert admitted
    assert engine.fingerprints['a'] != engine.fingerprints['b']
    assert engine.metrics['inheritance_replay_mismatches'] == 1
    assert engine.metrics['revoked_certificate_edges'] == 1
    assert not engine.edges and not any(engine.adjacency.values())
    paths = list(tmp_path.glob('replay-failures/*.pickle'))
    assert len(paths) == 1
    context = pickle.loads(paths[0].read_bytes())
    assert context['database'] == database and context['queries'] == engine.queries
    assert context['expected'] != context['actual']
    assert engine.assert_invariants()


def test_final_assertion_preserves_actual_and_stored_observations(tmp_path):
    engine = StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
                             random_dbs=0, budget_s=20)
    engine.evidence_dir = str(tmp_path)
    engine.add('a', 'SELECT COUNT(*) FROM t', compare=False)
    witness = engine.witnesses[0]
    witness.observations['a'] = Observation(('bag', 'rows', 1, ()), cardinality=('bag', 'cardinality', 1, 0))
    engine._reindex()
    with pytest.raises(AssertionError, match='full replay context'):
        engine.assert_invariants()
    contexts = [pickle.loads(p.read_bytes()) for p in tmp_path.glob('replay-failures/*.pickle')]
    assert any(c['reason'] == 'final_replay_mismatch' and 'a' in c['mismatches'] for c in contexts)


def test_cleanup_allowance_remains_call_wide_across_depths(monkeypatch):
    search = RepresentativeSearch(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
                                  'sqlite', persistent_worker=True)
    seen=[]
    def fake(query, representative, budget_s, bounds, pairs, augment, canonical, reserve=None):
        seen.append(reserve)
        return dict(status='domain_unsat',bounded_k=bounds[0],levels=[],wall_s=0)
    monkeypatch.setattr(search,'_search_persistent_level',fake)
    search.search('a','b',60,bounds=(1,2,4))
    assert seen == [1.0,1.0,1.0]
