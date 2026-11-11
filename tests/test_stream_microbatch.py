from seer.stream import StreamPartition
from seer.stream_microbatch import _pairs
from seer2.schema import Schema


def test_representative_microbatch_admits_only_replayed_witness_and_splits_once():
    engine = StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
                             random_dbs=0, budget_s=30)
    engine.add('a', 'SELECT COUNT(*) FROM t', compare=False)
    engine.add('b', 'SELECT COUNT(x) FROM t', compare=False)
    engine.add('c', 'SELECT COUNT(*) + 0 FROM t', compare=False)
    pairs = _pairs(engine)
    assert len(pairs) == 2
    assert {q for _, q, _, _ in pairs} == {'b', 'c'}
    candidate = {'t': [{'x': None}]}
    assert engine.install_batch([(candidate, ('a', 'b')),
                                 (candidate, ('a', 'b'))]) == 1
    assert engine.fingerprints['a'] == engine.fingerprints['c']
    assert engine.fingerprints['a'] != engine.fingerprints['b']
    assert engine.metrics['invalid_admitted_certificates'] == 0
    assert engine.assert_invariants()


def test_microbatch_rejects_nonseparating_and_sigma_invalid_databases():
    engine = StreamPartition(Schema('CREATE TABLE t(x INTEGER NOT NULL);', 'sqlite'),
                             random_dbs=0, budget_s=30)
    engine.add('a', 'SELECT COUNT(*) FROM t', compare=False)
    engine.add('b', 'SELECT COUNT(x) FROM t', compare=False)
    before = len(engine.witnesses)
    assert engine.install_batch([({'t': [{'x': None}]}, ('a', 'b')),
                                 ({'t': [{'x': 1}]}, ('a', 'b'))]) == 0
    assert len(engine.witnesses) == before
    assert engine.metrics['rejected_candidates'] == 2


def test_microbatch_reschedules_largest_current_class_and_skips_completed_pairs():
    class Partition:
        arrival = ['a', 'b', 'c', 'd', 'e', 'f']
        queries = {q: q for q in arrival}
        index = {'small': ['a', 'b'], 'large': ['c', 'd', 'e', 'f']}
        representatives = {'small': 'a', 'large': 'c'}

        def _path(self, a, b):
            return None

    engine = Partition()
    assert [(a, b) for a, b, _, _ in _pairs(engine)] == [
        ('c', 'd'), ('c', 'e'), ('c', 'f')]
    assert [(a, b) for a, b, _, _ in _pairs(engine, {('c', 'd')})] == [
        ('c', 'e'), ('c', 'f')]
    engine.index = {'split': ['c'], 'still_large': ['d', 'e', 'f'], 'small': ['a', 'b']}
    engine.representatives = {'split': 'c', 'still_large': 'd', 'small': 'a'}
    assert [(a, b) for a, b, _, _ in _pairs(engine, {('c', 'd')})] == [
        ('d', 'e'), ('d', 'f')]


def test_microbatch_inherits_only_scope_covered_followers_and_replays_target():
    engine = StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
                             random_dbs=0, budget_s=20, bounds=(1, 2))
    engine.add('a', 'SELECT x FROM t')
    engine.add('b', 'SELECT x FROM t WHERE 1=1')
    engine.add('c', 'SELECT x FROM t WHERE x IS NOT NULL', compare=False)
    assert engine.relation('b')['status'] == 'bounded'
    before = engine.metrics['small_db_executions']
    assert engine.install_batch([({'t': [{'x': None}]}, ('a', 'c'))]) == 1
    # Representative, c, verified follower, and fresh target replay.
    assert engine.metrics['small_db_executions'] - before == 5
    assert engine.fingerprints['a'] == engine.fingerprints['b'] != engine.fingerprints['c']
    assert engine.metrics['invalid_admitted_certificates'] == 0
    assert engine.assert_invariants()


def test_official_microbatch_counts_gain_and_overlap_after_class_split(monkeypatch, tmp_path):
    """Prior class migration must not make a later microbatch subtract overlap twice."""
    from seer.stream_persistent_microbatch import PersistentOfficialBatch
    from seereval.schema import Schema as EvalSchema, Table
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')

    def setup():
        e = StreamPartition(schema, random_dbs=0, budget_s=30, bounds=(1,))
        for q, sql in [('a', 'SELECT COUNT(*) FROM t'),
                       ('d', 'SELECT COUNT(*) FROM t'),
                       ('b', 'SELECT COUNT(x) FROM t'),
                       ('c', 'SELECT COUNT(DISTINCT x) FROM t')]:
            e.add(q, sql, compare=False)
        # The class proof tier's actual install entry: all four cross-class
        # pairs migrate together, not only the search target (a,b).
        assert e._install({'t': [{'x': None}]}, target=('a', 'b'), origin='polygon')
        e._compare('d', rep='a', use_quick=False)
        assert _pairs(e)[0][:2] == ('b', 'c')
        return e

    engine, sequential = setup(), setup()
    database = {'t': [{'x': 1}, {'x': 1}, {'x': None}]}
    before = dict(engine.fingerprints)
    eval_schema = EvalSchema('toy', [Table('t', [{'name': 'x', 'type': 'int'}])])
    session = PersistentOfficialBatch(engine, eval_schema, 'sqlite', tmp_path/'native', 0)
    replies = iter([
        {'kind': 'row', 'columns': ['Index','Schema','Refuted','Counterexample','Time','Iterations','Error'],
         'row': [1,'batch',True,repr({'t': [['x'],[1],[1],[None]]}),0.01,1,'']},
        {'kind': 'done'},
    ])
    sent = []
    monkeypatch.setattr(session, '_start', lambda: None)
    monkeypatch.setattr(session, '_send', sent.append)
    monkeypatch.setattr(session, '_read', lambda: next(replies))
    try:
        session.run_pending()
    finally:
        session.close()
    assert sent[-1] == {'action': 'stop'}
    assert sequential._install(database, target=('b','c'), origin='quick')
    # The second library distinguishes five pairs: four already separated by
    # the class split, and one new pair. The former code computed 1-4 == -3.
    assert engine.metrics['quick_separated_pairs'] == 1
    assert engine.metrics['polygon_library_separated_pairs'] == 4
    assert engine._lib_overlap_count == 4
    assert engine.fingerprints == sequential.fingerprints
    assert engine.raw_split_events() == sequential.raw_split_events()
    assert engine._lib_overlap_count == engine._raw_library_overlaps()[0]
    assert sum(before[a] == before[b] and engine.fingerprints[a] != engine.fingerprints[b]
               for i,a in enumerate(engine.arrival) for b in engine.arrival[i+1:]) == 1
    assert engine.assert_invariants(replay=True)
