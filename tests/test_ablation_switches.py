"""Experiment-card ablation switches must change the mechanism, not the flag.

One test per switch:
  fp_off      no arrival signature-index separation
  pool_off    every pair decision starts from the seed pool
  cert_off    every same-class member is decided directly
  bucket_off  no multi-bucket augmentation in a distinguishing search
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
from pathlib import Path

from seer.representative_search import RepresentativeSearch
from seer.stream_runner import run_stream
from seer2.schema import Schema

WORK = Path((SEER_ROOT + '/repro/dst-ablation-tests-v1'))


def schema():
    return Schema('CREATE TABLE t(x INTEGER, y INTEGER);', 'sqlite')


class FakeSearch:
    """Minimal search stand-in with the state StreamPartition snapshots."""

    def __init__(self, candidate_pairs=()):
        self.calls = []
        self.augments = []
        self.candidate_pairs = {frozenset(p) for p in candidate_pairs}
        self.queries, self.irs, self.unsupported = {}, {}, {}
        self.ir_order, self.levels, self.trace = [], {}, []
        self.domain_inputs, self.domain_revision = None, 0
        self.real = RepresentativeSearch(schema(), 'sqlite', persistent_worker=False)
        self.domain = self.real.domain

    def domain_identity(self):
        return self.real.domain_identity()

    def add_query(self, qid, sql):
        self.queries[qid] = sql

    def search(self, query, representative, budget_s, **kw):
        self.calls.append((query, representative))
        self.augments.append(kw.get('augment'))
        if frozenset((query, representative)) in self.candidate_pairs:
            return {'status': 'candidate', 'database': {'t': [{'x': 2, 'y': 2}]},
                    'levels': [{'k': 1, 'solver_calls': 1}], 'bounded_k': None,
                    'augment': kw.get('augment'), 'augment_rounds': 0,
                    'augment_extra_pairs': 0}
        return {'status': 'undecided', 'reason': 'test', 'levels': [],
                'bounded_k': None, 'augment': kw.get('augment'),
                'augment_rounds': 0, 'augment_extra_pairs': 0}

    def close_worker(self):
        self.real.close_worker()


def run(queries, search, **kw):
    tag = kw.pop('tag')
    return run_stream(schema(), queries, 'sqlite', 'sqlite', 30, 0,
                      native_runtime=WORK / 'none.json', evidence_dir=WORK / tag,
                      eval_schema=schema(), enable_prover=False, quick_sqlwitness=False,
                      schedule='arrival', bounds=(1,), per_call_s=1., search=search, **kw)[1]


def test_fp_off_never_credits_an_arrival_index_separation():
    # The second query has a different fingerprint on the two random seed
    # databases, so the default arrival drops it into a new class with no
    # decision. Without fingerprint grouping it must enter the decision path.
    queries = [('a', 'SELECT COUNT(*) FROM t WHERE x = 1'),
               ('c', 'SELECT COUNT(*) FROM t WHERE x = 2')]
    full = FakeSearch()
    result = run(queries, full, tag='fp-full')
    assert result['metrics']['fingerprint_arrival_separated_pairs'] > 0

    off = FakeSearch()
    result = run(queries, off, tag='fp-off', no_fingerprint_grouping=True)
    metrics = result['metrics']
    assert metrics['fingerprint_arrival_separated_pairs'] == 0
    assert metrics['first_fingerprint_separated_pairs'] == 0
    assert metrics['fingerprint_separated_pairs'] == 0
    assert metrics['pair_decision_requests'] >= 1


def test_pool_off_starts_every_pair_from_the_seed_pool():
    # a and b have the same fingerprint, so the arrival runs a pair decision.
    queries = [('a', 'SELECT COUNT(*) FROM t WHERE x = 1'),
               ('b', 'SELECT COUNT(*) FROM t WHERE x = 1 AND 1 = 1')]
    full = FakeSearch()
    run(queries, full, tag='pool-full')
    assert full.calls  # the default decision ran

    off = FakeSearch()
    result = run(queries, off, tag='pool-off', no_pool_reuse=True)
    metrics = result['metrics']
    assert metrics['cross_pair_reuse'] == 0
    assert metrics['pool_resets'] >= 1
    assert metrics['decision_pool_size'] == 3  # empty database + two seeds


def test_cert_off_decides_every_same_class_member_directly():
    # All three queries share one seed fingerprint; the default compares the
    # new arrival only with the class representative.
    queries = [('a', 'SELECT COUNT(*) FROM t WHERE x = 1'),
               ('b', 'SELECT COUNT(*) FROM t WHERE x = 1 AND 1 = 1'),
               ('c', 'SELECT COUNT(*) FROM t WHERE x = 1 AND x = 1')]
    full = FakeSearch()
    run(queries, full, tag='cert-full')
    assert {frozenset(p) for p in full.calls} == {
        frozenset(('b', 'a')), frozenset(('c', 'a'))}

    off = FakeSearch()
    result = run(queries, off, tag='cert-off', no_certificate_transfer=True)
    assert {frozenset(p) for p in off.calls} == {
        frozenset(('b', 'a')), frozenset(('c', 'a')), frozenset(('c', 'b'))}
    metrics = result['metrics']
    assert metrics['transferred_labels'] == 0
    assert metrics['inherited_labels'] == 0


def test_bucket_off_passes_no_augmentation_to_the_search():
    queries = [('a', 'SELECT COUNT(*) FROM t WHERE x = 1'),
               ('b', 'SELECT COUNT(*) FROM t WHERE x = 1 AND 1 = 1')]
    full = FakeSearch()
    run(queries, full, tag='bucket-full')
    assert full.augments and set(full.augments) == {3}

    off = FakeSearch()
    result = run(queries, off, tag='bucket-off', single_bucket_target=True)
    assert off.augments and set(off.augments) == {0}
    assert result['metrics']['augment_rounds'] == 0
    assert result['metrics']['augment_extra_pairs'] == 0
