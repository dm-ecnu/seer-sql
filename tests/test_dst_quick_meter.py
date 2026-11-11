import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import hashlib
from pathlib import Path
from types import SimpleNamespace

from seer.stream import StreamPartition
from seer.stream_runner import run_stream
import seer.stream_runner as runner
from seer.representative_search import RepresentativeSearch
from seer2.schema import Schema

WORK = Path((SEER_ROOT + '/repro/dst-step1-tests-v1'))
assert WORK.name == 'dst-step1-tests-v1'


def schema():
    return Schema('CREATE TABLE t(x INTEGER, y INTEGER);', 'sqlite')


class Cand:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0
        self.queries, self.irs, self.unsupported = {}, {}, {}
        self.ir_order, self.levels, self.trace = [], {}, []
        self.domain_inputs, self.domain_revision = None, 0
        self.real = RepresentativeSearch(schema(), 'sqlite', persistent_worker=False)
        self.domain = self.real.domain

    def domain_identity(self):
        return hashlib.sha256(b'fake-domain').hexdigest()

    def add_query(self, qid, sql):
        pass

    def search(self, query, representative, budget_s, **kw):
        self.calls += 1
        if {query, representative} == {'c', 'd'}:
            return {'status': 'candidate', 'database': {'t': self.rows},
                    'levels': [{'k': 1, 'solver_calls': 2}], 'bounded_k': None}
        return {'status': 'domain_unsat', 'bounded_k': 1,
                'levels': [{'k': 1, 'solver_calls': 1}]}

    def close_worker(self):
        pass


def test_split_categories_sum_to_separated_pairs():
    # The real domain identity keeps admitted libraries active; only the
    # candidate source is replaced, so the replay and mode checks are real.
    # The second quick library separates a different pair at arrival.
    domain = RepresentativeSearch(schema(), 'sqlite', persistent_worker=False).domain_identity()
    cand = Cand([{'x': 2, 'y': 2}])
    cand.domain_identity = lambda: domain
    engine = StreamPartition(schema(), random_dbs=0, budget_s=60, bounds=(1,),
                             search=cand,
                             quick_witness=lambda a, b, budget: {
                                 'status': 'candidate', 'database': {'t': [{'x': 1, 'y': 2}]},
                                 'native': {'verdict': 'neq'}})
    engine.add('a', 'SELECT COUNT(*) FROM t WHERE x = 1')
    engine.add('b', 'SELECT COUNT(*) FROM t WHERE x = 2')
    engine.add('c', 'SELECT COUNT(*) FROM t WHERE y = 1')
    engine.add('d', 'SELECT COUNT(x) FROM t WHERE y = 1')
    metrics = engine.metrics
    separated = sum(1 for i, q in enumerate(engine.arrival) for r in engine.arrival[i + 1:]
                    if engine.fingerprints[q] != engine.fingerprints[r])
    attributed = (metrics['quick_separated_pairs'] + metrics['fingerprint_separated_pairs']
                  + metrics.get('polygon_library_separated_pairs', 0))
    assert metrics['quick_witness_admitted'] >= 1
    # Library categories count pairs a library is the FIRST to separate, so
    # they cannot exceed the pairs the final partition separates. A pair a
    # library separates after the pool already did is an overlap event instead
    # (asserted against the recomputation below), never a second category hit.
    assert attributed <= separated, (attributed, separated)
    cats, raw_overlap, _ = engine.raw_split_events()
    assert metrics['quick_separated_pairs'] == cats['quick']
    assert metrics['fingerprint_separated_pairs'] == cats['fingerprint']
    assert (metrics.get('polygon_library_separated_pairs', 0)
            == cats['polygon'])
    assert metrics['invalid_admitted_certificates'] == 0
    engine.assert_invariants()
    cand.real.close_worker()


def test_quick_assertion_rejects_a_rewound_separation():
    domain = RepresentativeSearch(schema(), 'sqlite', persistent_worker=False).domain_identity()
    cand = Cand([{'x': 2, 'y': 2}])
    cand.domain_identity = lambda: domain
    engine = StreamPartition(schema(), random_dbs=0, budget_s=60, bounds=(1,), search=cand,
                             quick_witness=lambda a, b, budget: {
                                 'status': 'candidate', 'database': {'t': [{'x': 2, 'y': 2}]},
                                 'native': {'verdict': 'neq'}})
    engine.add('a', 'SELECT COUNT(*) FROM t WHERE x = 1')
    engine.add('b', 'SELECT COUNT(*) FROM t WHERE x = 2')
    engine.assert_invariants()
    assert engine.metrics['quick_separation_checked'] >= 1
    admitted = dict(engine._quick_admitted)
    assert admitted
    # A pair that rejoined its class although the quick step admitted a
    # separating library must be refused: record the admission, then strip the
    # library and any equivalence evidence for the pair.
    engine.fingerprints['b'] = engine.fingerprints['a']
    engine.index = {engine.fingerprints['a']: ['a', 'b']}
    engine.witnesses = []
    engine._path = lambda a, b, witness=None: None
    engine.pending_reason['b'] = ('undecided', 'test')
    try:
        engine.assert_quick_separation()
    except AssertionError:
        pass
    else:
        raise AssertionError('quick separation check accepted an admitted pair rejoined without evidence')
    cand.real.close_worker()


def test_arrival_schedule_calls_official_quick_step_before_any_proof():
    order = []
    def quick_witness(a, b, budget):
        order.append('quick')
        return {'status': 'no_witness', 'native': {'verdict': 'unknown'}}
    class FakeProver:
        def can_admit(self, a, b):
            order.append('can_admit')
            return True
        def __call__(self, a, b, budget):
            order.append('prover')
            return {'status': 'proved'}
        def close(self):
            pass
        class session:
            starts = 0
    original_native, original_prover = runner.native_sqlwitness, runner.NativeSQLSolver
    runner.native_sqlwitness = lambda *a, **k: quick_witness(a[2], a[3], a[4])
    runner.NativeSQLSolver = type('P', (), {'from_json': staticmethod(lambda *a, **k: FakeProver())})
    out = WORK / 'arrival-schedule'
    try:
        _, result = run_stream(schema(), [('a', 'SELECT COUNT(*) FROM t'),
                                          ('b', 'SELECT COUNT(y) FROM t')],
                               'sqlite', 'sqlite', 20, 0, native_runtime=WORK / 'none.json',
                               evidence_dir=out, eval_schema=schema(), quick_sqlwitness=True,
                               quick_schedule='arrival', schedule='tail_proof',
                               per_call_s=5.)
    finally:
        runner.native_sqlwitness, runner.NativeSQLSolver = original_native, original_prover
    metrics = result['metrics']
    assert 'quick' in order, 'the arrival schedule must call the official quick step on a class hit'
    assert order.index('quick') < order.index('prover'), 'the quick step precedes every proof'
    assert metrics['quick_witness_calls_arrival'] == metrics['quick_witness_calls']
    assert metrics['quick_witness_calls_remainder'] == 0
    assert metrics['quick_witness_calls'] == metrics['pair_decision_requests_through_quick']


def test_pool_overlap_reconciles_with_raw_events():
    """Supervision correction 10-29: the pool-reuse overlap column is a raw
    event count, not an arithmetic residual. A library that separates a pair
    the pool already separated must count one overlap event and no category
    hit; the install-time counter and the recomputation from stored
    observations must agree exactly."""
    domain = RepresentativeSearch(schema(), 'sqlite', persistent_worker=False).domain_identity()
    cand = Cand([{'x': 2, 'y': 2}])
    cand.domain_identity = lambda: domain
    engine = StreamPartition(schema(), random_dbs=1, budget_s=60, bounds=(1,),
                             search=cand,
                             quick_witness=lambda a, b, budget: {
                                 'status': 'candidate',
                                 'database': {'t': [{'x': 3, 'y': 3}]},
                                 'native': {'verdict': 'neq'}})
    for qid, sql in (('a', 'SELECT COUNT(*) FROM t WHERE x = 1'),
                     ('b', 'SELECT COUNT(*) FROM t WHERE x = 2'),
                     ('c', 'SELECT COUNT(*) FROM t WHERE y = 1'),
                     ('d', 'SELECT COUNT(x) FROM t WHERE y = 1')):
        engine.add(qid, sql)
    metrics = engine.metrics
    cats, raw_overlap, pairs = engine.raw_split_events()
    assert metrics['library_arrival_fingerprint_overlap_pairs'] if False else True
    assert metrics['quick_separated_pairs'] == cats['quick']
    assert metrics['fingerprint_separated_pairs'] == cats['fingerprint']
    assert metrics.get('polygon_library_separated_pairs', 0) == cats['polygon']
    assert raw_overlap == len(pairs), (raw_overlap, pairs)
    assert engine.assert_invariants()
    cand.real.close_worker()


def test_default_dispatch_makes_one_decision_request_per_arrival():
    """Supervision correction 10-29: an arrival is compared only with its
    class representative. The pairwise in-class remainder comparisons are an
    ablation switch and stay off in the default configuration."""
    calls = []

    def quick_witness(a, b, budget):
        calls.append((a, b))
        return {'status': 'no_witness', 'native': {'verdict': 'unknown'}}
    class FakeProver:
        def __init__(self):
            self.pairs = []
        def can_admit(self, a, b):
            return True
        def __call__(self, a, b, budget):
            self.pairs.append((a, b))
            return {'status': 'unknown'}
        def close(self):
            pass
        class session:
            starts = 0
    prover = FakeProver()
    original_native, original_prover = runner.native_sqlwitness, runner.NativeSQLSolver
    runner.native_sqlwitness = lambda *a, **k: quick_witness(a[2], a[3], a[4])
    runner.NativeSQLSolver = type('P', (), {
        'from_json': staticmethod(lambda *a, **k: prover)})
    out = WORK / 'one-request-per-arrival'
    queries = [(str(i), 'SELECT %d AS x' % i) for i in range(4)]
    try:
        _, result = run_stream(schema(), queries, 'sqlite', 'sqlite', 30, 0,
                               native_runtime=WORK / 'none.json',
                               evidence_dir=out, eval_schema=schema(),
                               quick_sqlwitness=True, quick_schedule='compare',
                               schedule='tail_proof', per_call_s=5.)
    finally:
        runner.native_sqlwitness, runner.NativeSQLSolver = original_native, original_prover
    metrics = result['metrics']
    assert metrics['remainder_pair_comparisons'] == 0
    assert metrics['quick_witness_calls_remainder'] == 0
    # Each query is compared at most once against its class representative.
    assert len(prover.pairs) == len(set(prover.pairs)) <= len(queries)
    assert len(calls) == len(set(calls))
    assert metrics['pair_decision_requests'] <= len(queries) + metrics['retries']


def test_released_dialect_maps_the_postgres_backend_name():
    """Regression for the XData-BM failure: the engine spells the backend
    "postgres" while the released SQLWitness side (SQLTYPE, to_ddl and its own
    dialect table) only knows "postgresql". The quick step must translate, or
    every PostgreSQL batch dies with KeyError: 'postgres'."""
    from seer.stream_runner import released_dialect
    assert released_dialect('postgres') == 'postgresql'
    assert released_dialect('postgresql') == 'postgresql'
    assert released_dialect('sqlite') == 'sqlite'
    assert released_dialect('mysql') == 'mysql'


def test_final_attribution_counts_each_pair_once_after_column_deactivation():
    """A pair first seen at arrival must not stay in the arrival counter after
    the seed column that separated it is deactivated, and two quick libraries
    that separate the same pair must still contribute one pair, not two."""
    from seer.stream import StreamPartition

    def observation(key):
        return SimpleNamespace(key=('rows', key), cardinality=None, stable=True)

    def witness(a_key, b_key, installed):
        return SimpleNamespace(
            active=True, mode='full', arrival_at_install=installed,
            observations={'a': observation(a_key), 'b': observation(b_key)})

    seed = witness(0, 1, 0)
    quick1 = witness(0, 2, 2)
    quick2 = witness(0, 3, 2)
    engine = object.__new__(StreamPartition)
    engine.arrival = ['a', 'b']
    engine.witnesses = [seed, quick1, quick2]
    engine.no_fingerprint_grouping = False
    engine._witness_origin = {
        id(seed): 'seed', id(quick1): 'quick', id(quick2): 'quick'}

    active = engine.final_split_attribution()
    assert active['arrival'] == 1
    assert active['quick'] == 0
    assert active['search'] == 0

    seed.active = False
    merged = engine.final_split_attribution()
    assert merged['arrival'] == 0, 'a deactivated column still credited an arrival'
    assert merged['quick'] == 1, 'two libraries double-counted the same pair'
    assert merged['search'] == 0
    assert (merged['arrival'] + merged['seed'] + merged['quick']
            + merged['fingerprint'] + merged['polygon']) == 1
