import decimal
import time
import pytest
from seer.stream import StreamPartition, value_key, result_key, Edge
from seer2.schema import Schema
from seer2.backends import QueryError


def stream(**kw):
    return StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
                           random_dbs=0, budget_s=10, bounds=(1, 2), **kw)


def test_keys_are_exact_transitive_and_preserve_bags_whitespace_and_width():
    a, b = [value_key(x) for x in (1.234561, 1.234562)]
    assert a == b
    assert value_key(decimal.Decimal('1.234561')) != value_key(decimal.Decimal('1.234562'))
    assert value_key(decimal.Decimal('29000.01')) != value_key(decimal.Decimal('29000.02'))
    assert value_key(decimal.Decimal('12345678901234567890123456789')) != value_key(decimal.Decimal('12345678901234567890123456790'))
    assert value_key(29000.01) != value_key(29000.02)
    assert value_key(1) == value_key(1.) == value_key(decimal.Decimal('1'))
    assert value_key('x ') != value_key('x')
    assert value_key(b'x') != value_key('78')
    assert result_key([(1,), (2,)], 1, False) == result_key([(2,), (1,)], 1, False)
    assert result_key([(1,), (1,)], 1, False) != result_key([(1,)], 1, False)
    assert result_key([], 1, False) != result_key([], 2, False)
    assert result_key([], 1, True) != result_key([], 1, False)
    assert result_key(QueryError('a'), 0, False) != result_key(QueryError('b'), 0, False)


def test_fingerprint_miss_never_calls_solver_and_order_is_separate():
    s = stream()
    s.add('a', 'SELECT 1')
    s.add('b', 'SELECT 2')
    s.add('c', 'SELECT 1 ORDER BY 1')
    assert len(s.index) == 3
    assert s.metrics['solver_calls'] == 0
    assert s.assert_invariants()


def test_null_count_witness_is_fresh_replayed_and_shared():
    s = stream()
    s.add('a', 'SELECT COUNT(*) FROM t')
    s.add('b', 'SELECT COUNT(x) FROM t')
    assert len(s.index) == 2
    assert len(s.witnesses) == 2
    assert any(r['x'] is None for r in s.witnesses[-1].database['t'])
    assert s.metrics['solver_calls'] > 0
    s.add('c', 'SELECT COUNT(x) FROM t WHERE 1=1')
    assert s.fingerprints['b'] == s.fingerprints['c'] != s.fingerprints['a']
    assert s.relation('c')['status'] == 'bounded'
    assert s.assert_invariants()


def test_shorter_bounded_member_replaces_rep_and_covered_followers_are_verified():
    s = stream()
    s.add('a', 'SELECT x FROM t WHERE 1=1')
    s.add('b', 'SELECT x FROM t')
    assert list(s.representatives.values()) == ['b']
    assert s.relation('a')['status'] == 'bounded'
    before = s.metrics['small_db_executions']
    assert s._install({'t': [{'x': None}]})
    assert s.metrics['small_db_executions'] - before == 2
    assert s.witnesses[-1].observations['a'] is not s.witnesses[-1].observations['b']
    before = s.metrics['small_db_executions']
    assert s._install({'t': [{'x': 0}, {'x': 1}, {'x': 2}]})
    assert s.metrics['small_db_executions'] - before == 2
    assert s.assert_invariants()


def test_domain_change_revokes_old_bounded_edge_without_false_split():
    s = StreamPartition(Schema('CREATE TABLE t(x TEXT);', 'sqlite'),
                        random_dbs=0, bounds=(1,), budget_s=10)
    s.add('a', 'SELECT x FROM t WHERE 1=1')
    s.add('b', 'SELECT x FROM t')
    assert s.relation('a')['status'] == 'bounded'
    domain = s.edges[0].domain
    s.add('c', "SELECT x FROM t WHERE x='new-literal-unseen'")
    assert s.search.domain_identity() != domain
    assert s.relation('a', 'b')['status'] == 'undecided'
    s.retry_pending()
    assert s.relation('a', 'b')['status'] == 'bounded'
    assert s.assert_invariants()


def test_bad_schema_or_nonseparating_witness_is_not_admitted():
    s = StreamPartition(Schema('CREATE TABLE t(x INTEGER NOT NULL);', 'sqlite'),
                        random_dbs=0, bounds=(1,), budget_s=10)
    s.add('a', 'SELECT x FROM t');s.add('b', 'SELECT x FROM t')
    n = len(s.witnesses)
    assert not s._install({'t': [{'x': None}]}, target=('a', 'b'))
    assert not s._install({'t': [{'x': 1}]}, target=('a', 'b'))
    assert len(s.witnesses) == n and s.metrics['rejected_candidates'] == 2
    assert s.metrics['invalid_admitted_certificates'] == 0


def test_arrival_retry_uses_shared_batch_deadline():
    s = stream()
    s.add('a', 'SELECT x FROM t', compare=False)
    s.add('b', 'SELECT x FROM t WHERE 1=1', compare=False)
    s.deadline = time.perf_counter() - 1
    s._compare('b')
    assert s.relation('b')['status'] == 'undecided'
    assert s.metrics['solver_calls'] == 0
    s.deadline = time.perf_counter() + 5
    s.retry_pending()
    assert s.relation('b')['status'] == 'bounded'
    assert s.metrics['retries'] == 1


def test_native_eq_without_semantics_gate_is_not_proved():
    s = stream(prover=lambda *args: {'status': 'native_eq', 'evidence': {'receipt': 'test'}})
    s.add('a', 'SELECT x FROM t');s.add('b', 'SELECT x FROM t')
    assert not any(e.status == 'proved' for e in s.edges)
    assert s.relation('b')['status'] == 'bounded'
    assert s.metrics['prover_calls'] == 1


def test_native_call_skipped_when_receipt_cannot_be_admitted():
    class UnadmittableProver:
        def can_admit(self, a, b):
            return False

        def __call__(self, *args):
            raise AssertionError('inadmissible native call must not run')

    s = stream(prover=UnadmittableProver())
    s.add('a', 'SELECT x FROM t')
    s.add('b', 'SELECT x FROM t WHERE 1=1')
    assert s.metrics['prover_calls'] == 0
    assert s.metrics['prover_skipped_inadmissible'] == 1
    assert s.relation('b')['status'] == 'bounded'
    assert s.assert_invariants()


def test_repeated_deep_search_for_one_representative_uses_verified_prefix(monkeypatch):
    s = StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'), random_dbs=0, budget_s=10, bounds=None)
    s.add('a', 'SELECT x FROM t', compare=False)
    s.add('b', 'SELECT x FROM t WHERE 1=1', compare=False)
    seen = []
    def fake_search(q, rep, budget, bounds=None, **kwargs):
        seen.append(bounds)
        return {'status': 'undecided', 'reason': 'solver_unknown',
                'bounded_k': 4, 'domain_sha256': s.search.domain_identity(),
                'levels': [], 'comparison_contract': 'canonical-exact-decimal-float3dp-list-bag-errors-v3'}
    monkeypatch.setattr(s.search, 'search', fake_search)
    s._compare('b')
    assert s._rep_depth_caps['a'] == 4
    s.add('c', 'SELECT x FROM t WHERE 2=2', compare=False)
    s._compare('c')
    assert seen == [None, (1, 2, 4)]
    assert s.metrics['depth_caps_learned'] == 1
    assert s.metrics['depth_caps_applied'] == 1
    assert s.assert_invariants()


def test_deep_failures_across_representatives_bound_later_search(monkeypatch):
    s = StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
                        random_dbs=0, budget_s=10, bounds=None)
    for q, sql in [('a', 'SELECT x FROM t'), ('b', 'SELECT x FROM t WHERE 1=1'),
                   ('c', 'SELECT x FROM t WHERE 2=2'), ('d', 'SELECT x FROM t WHERE 3=3')]:
        s.add(q, sql, compare=False)
    seen = []
    def fake_search(q, rep, budget, bounds=None, **kwargs):
        seen.append(bounds)
        return {'status': 'undecided', 'reason': 'solver_unknown',
                'bounded_k': 4, 'domain_sha256': s.search.domain_identity(),
                'levels': [], 'comparison_contract': 'canonical-exact-decimal-float3dp-list-bag-errors-v3'}
    monkeypatch.setattr(s.search, 'search', fake_search)
    s._compare('b', rep='a', force=True)
    s._compare('c', rep='b', force=True)
    s._compare('d', rep='c', force=True)
    assert seen == [None, None, (1, 2, 4)]
    assert s.metrics['global_depth_caps_learned'] == 1
    assert s.metrics['global_depth_caps_applied'] == 1
    s.deepen_deferred()
    assert seen == [None, None, (1, 2, 4)]
    assert s.metrics["deep_searches_skipped"] == 1
    assert s.metrics['deferred_deepening_calls'] == 1
    s.deadline = time.perf_counter() + s.finalization_reserve_s / 2
    s.deepen_deferred()
    assert s.metrics['deferred_deepening_calls'] == 1
    assert s.assert_invariants()


def test_order_tie_column_never_creates_arbitrary_order_certificate():
    s = stream()
    s.add('a', 'SELECT x FROM t ORDER BY 1')
    s.add('b', 'SELECT x FROM t ORDER BY 0+0 LIMIT 1')
    s._install({'t': [{'x': 1}, {'x': 2}]})
    assert s.witnesses[-1].active and s.witnesses[-1].mode == 'cardinality'
    assert s.witnesses[-1].reason == 'order_or_limit_tie'
    assert s.assert_invariants()


def pair_sets(s):
    ids = sorted(s.queries)
    out = {'bounded': set(), 'separated': set(), 'proved': set()}
    for i, a in enumerate(ids):
        for b in ids[i+1:]:
            pair = (a, b)
            if s.fingerprints[a] != s.fingerprints[b]:
                out['separated'].add(pair)
            else:
                r = s.relation(a, b)
                if r['status'] in out:
                    out[r['status']].add(pair)
    return out


def test_three_actual_arrival_orders_agree_on_small_complete_fragment():
    queries = [('a', 'SELECT x FROM t'), ('b', 'SELECT x FROM t WHERE 1=1'),
               ('c', 'SELECT DISTINCT x FROM t'), ('d', 'SELECT COUNT(*) FROM t'),
               ('e', 'SELECT COUNT(x) FROM t')]
    outputs = []
    for order in (queries, list(reversed(queries)), [queries[i] for i in (2, 0, 4, 1, 3)]):
        s = stream()
        for q, sql in order:
            s.add(q, sql)
        s.retry_pending()
        assert s.assert_invariants()
        outputs.append(pair_sets(s))
    assert outputs[0] == outputs[1] == outputs[2]


def test_meter_snapshot_has_actual_cpu_wall_memory_and_pool_events():
    s = stream();s.add('a', 'SELECT 1');s.add('b', 'SELECT 2')
    r = s.snapshot()
    assert r['metrics']['cpu_s'] > 0 and r['metrics']['wall_s'] > 0
    assert r['metrics']['peak_rss_kib'] > 0 and r['metrics']['small_db_executions'] > 0
    assert r['events'][0]['event'] == 'pool_added'


def test_bounded_follower_is_reexecuted_outside_integer_representation():
    s = stream()
    s.add('a', 'SELECT x FROM t WHERE 1=1');s.add('b', 'SELECT x FROM t')
    before = s.metrics['small_db_executions']
    assert s._install({'t': [{'x': 2**40}]})
    assert s.metrics['small_db_executions'] - before == 2
    assert s.assert_invariants()


def test_bare_limit_insertion_order_is_not_a_certificate():
    s = stream()
    s.add('a', 'SELECT x FROM t LIMIT 1')
    s._install({'t': [{'x': 1}, {'x': 2}]})
    assert s.witnesses[-1].active and s.witnesses[-1].mode == 'cardinality'
    assert s.witnesses[-1].reason == 'insertion_order_sensitive'
    assert s.assert_invariants()


def test_exact_real_unsat_is_not_admitted_as_machine_float_equivalence():
    s = StreamPartition(Schema('CREATE TABLE t(x DOUBLE);', 'sqlite'),
                        random_dbs=0, bounds=(1,), budget_s=5)
    s.add('a', 'SELECT x FROM t');s.add('b', 'SELECT x FROM t WHERE 1=1')
    assert s.relation('b')['status'] == 'undecided'
    assert not s.edges


def test_fingerprint_miss_does_not_rebuild_all_previous_queries(monkeypatch):
    s = stream()
    def fail():
        raise AssertionError('whole-partition scan on normal stream insertion')
    monkeypatch.setattr(s, '_reindex', fail)
    s.add('a', 'SELECT 1');s.add('b', 'SELECT 2')
    assert len(s.index) == 2


def test_limit_cardinality_certificate_survives_unstable_surviving_values():
    s = stream()
    s.add('a', 'SELECT x FROM t LIMIT 2');s.add('b', 'SELECT x FROM t')
    assert s._install({'t': [{'x': 1}, {'x': 2}, {'x': 3}]}, target=('a', 'b'))
    assert s.fingerprints['a'] != s.fingerprints['b']
    assert s.witnesses[-1].mode == 'cardinality'
    assert s.assert_invariants()


def test_ir_hash_consing_survives_value_domain_growth():
    from seer.ir import walk_rel
    from seer.representative_search import RepresentativeSearch
    search = RepresentativeSearch(Schema('CREATE TABLE t(x TEXT);', 'sqlite'), 'sqlite')
    search.add_query('a', 'SELECT x FROM t')
    search.add_query('b', 'SELECT x FROM t')
    root = search.irs['a']
    assert search.irs['b'] is root
    old_domain = search.domain_identity()
    search.add_query('c', "SELECT x FROM t WHERE x = 'a-new-literal'")
    assert search.domain_identity() != old_domain
    search.add_query('d', 'SELECT x FROM t')
    assert search.irs['d'] is root
    scans = [next(r for r in walk_rel(search.irs[q]) if r.op == 'scan')
             for q in ('a', 'c', 'd')]
    assert scans[0] is scans[1] is scans[2]


def test_two_deep_hard_cutoffs_stop_retrying(monkeypatch):
    s = StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
                        random_dbs=0, budget_s=10, bounds=None)
    s.add('a', 'SELECT x FROM t', compare=False)
    s.add('b', 'SELECT x FROM t WHERE 1=1', compare=False)
    calls = []
    def hard_cutoff(*args, **kwargs):
        calls.append(1)
        return {'status': 'undecided', 'reason': 'hard_timeout',
                'levels': [], 'bounded_k': None}
    monkeypatch.setattr(s.search, 'search', hard_cutoff)
    for _ in range(3):
        s._compare('b', rep='a', force=True, deepen=True)
    assert len(calls) == 2
    assert s.metrics['deep_searches_skipped'] == 1
    assert s.metrics['invalid_admitted_certificates'] == 0
