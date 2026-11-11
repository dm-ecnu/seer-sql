import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import pytest
from seer.stream import StreamPartition
from seer.stream_class import prove_classes
from seer.stream_runner import run_stream
from seer.class_metrics import depth_counts
from seer.class_ablation import decision_pool
from seer.stream_microbatch import _pairs
from seer2.schema import Schema


def engine(**kw):
    e = StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
        random_dbs=0, budget_s=30, per_call_s=10, bounds=(1, 2), **kw)
    e._class_arrival_only = True
    e._class_arrival_pairs = []
    for q in ('a', 'b', 'c', 'd'):
        e.add(q, 'SELECT x FROM t', compare=False)
    return e


def test_class_off_singletons_share_context_and_same_k_labels():
    full = engine(); off = engine()
    prove_classes(full); prove_classes(off, class_off=True)
    assert full.metrics['class_contexts'] == off.metrics['class_contexts'] == 1
    assert full.metrics['class_checks'] == 2
    assert off.metrics['class_checks'] == 6
    calls = [e['receipt'] for e in off.events if e.get('event') == 'class_bounded_search']
    assert {(r['members'][0], r['k']) for r in calls} == {
        (q, k) for q in ('b', 'c', 'd') for k in (1, 2)}
    assert all(len(r['members']) == 1 for r in calls)
    assert sum(not r['encoding_reused'] for r in calls) == 2
    for q in ('b', 'c', 'd'):
        assert full.relation(q)['k'] == off.relation(q)['k'] == 2
    full.assert_invariants(replay=True); off.assert_invariants(replay=True)


def test_class_off_runner_records_depth_counts_and_preserves_arrival(tmp_path):
    _, result = run_stream(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
        [(q, 'SELECT x FROM t') for q in ('a', 'b', 'c')], 'sqlite', 'sqlite',
        20, 0, None, tmp_path/'native', schedule='class_bounded', class_off=True,
        enable_prover=False, random_dbs=0, bounds=(1, 2))
    assert result['class_off']
    assert result['class_depth_counts']['1']['block_queries'] == 2
    assert result['class_depth_counts']['2']['unsat'] == 2
    boundary = next(i for i, e in enumerate(result['events']) if e['event'] == 'stream_arrivals_complete')
    assert not any(e['event'] in ('class_bounded_search', 'representative_search')
                   for e in result['events'][:boundary])


def test_transfer_off_class_schedule_checks_every_pair_directly():
    e = engine(no_certificate_transfer=True)
    # No legacy arrival prover in this mechanism test.
    e.events.clear()
    prove_classes(e)
    direct = {frozenset((x.a, x.b)) for x in e.edges if x.status == 'bounded' and x.k == 2}
    assert direct == {frozenset((a, b)) for i, a in enumerate(e.arrival) for b in e.arrival[i+1:]}
    assert e.count_transferred_labels() == 0
    e.assert_invariants(replay=True)


def test_index_off_queues_cross_class_official_pairs_without_arrival_polygon(tmp_path, monkeypatch):
    from seer.stream_persistent_microbatch import PersistentOfficialBatch
    observed = []
    def pending(self, min_pairs=1):
        pairs = _pairs(self.engine, self.seen_pairs)
        observed.extend((a, b) for a, b, _, _ in pairs)
        self.seen_pairs.update((a, b) for a, b, _, _ in pairs)
    monkeypatch.setattr(PersistentOfficialBatch, 'run_pending', pending)
    _, result = run_stream(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
        [('a', 'SELECT 1'), ('b', 'SELECT 2')], 'sqlite', 'sqlite', 20, 0,
        None, tmp_path/'native', schedule='class_bounded', no_fingerprint_grouping=True,
        quick_sqlwitness=True, eval_schema=object(), enable_prover=False, random_dbs=0, bounds=(1,))
    assert observed == [('a', 'b')]
    assert not any(e['event'] == 'representative_search' for e in result['events'])
    assert result['metrics']['fingerprint_arrival_separated_pairs'] == 0


def test_pool_off_hides_old_libraries_from_selection_and_class_context():
    e = StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
        random_dbs=0, budget_s=30, bounds=(1,), no_pool_reuse=True)
    e.add('a', 'SELECT COUNT(*) FROM t', compare=False)
    e.add('b', 'SELECT COUNT(x) FROM t', compare=False)
    assert e._install({'t': [{'x': None}]}, target=('a', 'b'), origin='quick')
    assert _pairs(e) == []
    with decision_pool(e, 'test'):
        assert len(_pairs(e)) == 1
        assert len(e.witnesses) == 1
    assert e.fingerprints['a'] != e.fingerprints['b']
    prove_classes(e)
    assert e.metrics['class_checks'] >= 1
    assert e.metrics['pool_resets'] >= 2
    assert e.metrics['cross_pair_reuse'] == 0
    e.assert_invariants(replay=True)


def test_index_off_old_library_is_not_credited_as_later_arrival_index_gain():
    e=StreamPartition(Schema('CREATE TABLE t(x INTEGER);','sqlite'),
        random_dbs=0,budget_s=20,no_fingerprint_grouping=True)
    e._class_arrival_only=True;e._class_arrival_pairs=[]
    e.add('a','SELECT COUNT(*) FROM t',compare=False)
    e.add('b','SELECT COUNT(x) FROM t',compare=False)
    assert e._install({'t':[{'x':None}]},target=('a','b'),origin='quick')
    e.add('c','SELECT COUNT(x) FROM t WHERE TRUE',compare=False)
    attribution=e.final_split_attribution()
    assert attribution['arrival']==attribution['fingerprint']==0
    assert attribution['quick']==2
    e._recompute_attribution()
    e.assert_invariants(replay=True)


def test_depth_counts_keep_unknown_unsupported_and_guard_outcomes_separate():
    receipts = [dict(k=1, status='candidate', solver_calls=1),
        dict(k=1, status='domain_unsat', solver_calls=2),
        dict(k=2, status='undecided', reason='solver_unknown', solver_calls=1),
        dict(k=2, status='undecided', reason='class_hard_timeout', solver_calls=0),
        dict(k=2, status='unsupported', solver_calls=0),
        dict(k=2, status='reduce_chunk', solver_calls=0)]
    rows = depth_counts([dict(event='class_bounded_search', receipt=r) for r in receipts], 5)
    assert rows['1']['sat'] == rows['1']['unsat'] == 1
    assert rows['2']['unknown'] == rows['2']['timeout'] == rows['2']['unsupported'] == 1
    assert rows['2']['solver_calls_incomplete']
    with pytest.raises(AssertionError):
        depth_counts([], 1)


def test_default_proof_matches_frozen_full_trace():
    import importlib.util
    from pathlib import Path
    path=Path((SEER_ROOT + '/seer-c64-class-bounded-wt/src/seer/stream_class.py'))
    if not path.exists():pytest.skip('frozen server worktree required')
    spec=importlib.util.spec_from_file_location('seer.frozen_class_proof',path)
    old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    a,b=engine(),engine();old.prove_classes(a);prove_classes(b)
    assert a.fingerprints==b.fingerprints
    assert [(e.a,e.b,e.status,e.k) for e in a.edges]==[(e.a,e.b,e.status,e.k) for e in b.edges]
    def calls(e):
        return [(x['receipt']['members'],x['receipt']['k'],x['receipt']['status'])
                for x in e.events if x.get('event')=='class_bounded_search']
    assert calls(a)==calls(b)
