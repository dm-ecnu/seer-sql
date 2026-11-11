from argparse import Namespace
import importlib.util
from pathlib import Path

import pytest


path = Path(__file__).resolve().parents[1] / 'scripts/run_stream_batch.py'
spec = importlib.util.spec_from_file_location('run_stream_batch', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_original_pair_deadline_preserves_full_reference_denominator():
    batch = {'dataset': 'leetcode_verieql_original_batches',
             'meta': {'original_pair_count': 2},
             'queries': [{'qid': 'gold', 'role': 'reference'},
                         {'qid': 'c0', 'role': 'candidate'},
                         {'qid': 'c1', 'role': 'candidate'}]}
    for deadline in (60., 600.):
        a = Namespace(method='seer_stream', budget=None,
                      original_pair_timeout_s=deadline)
        module.resolve_budget(a, batch)
        assert a.per_call_s == deadline
        assert a.budget == 2 * deadline
    a = Namespace(method='seer_stream', budget=None,
                  original_pair_timeout_s=600.)
    batch['queries'].pop()
    with pytest.raises(ValueError, match='denominator changed'):
        module.resolve_budget(a, batch)


def test_existing_batch_budget_is_unchanged():
    a = Namespace(method='seer_stream', budget=None,
                  original_pair_timeout_s=None)
    module.resolve_budget(a, {})
    assert (a.budget, a.per_call_s) == (600., 60.)
