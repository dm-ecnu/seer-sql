from seer.stream_runner import run_stream, run_sqlwitness
from seer2.schema import Schema


QUERIES = [(str(i), f'SELECT {i} AS x') for i in range(6)]


def _same_internal_pool(a, b):
    assert [w.database for w in a.witnesses] == [w.database for w in b.witnesses]
    assert a.search.seed == b.search.seed == 0
    assert a.arrival != b.arrival
    assert sorted(a.arrival) == sorted(b.arrival)


def test_stream_seed_changes_only_arrival_order():
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')
    runs = [run_stream(schema, QUERIES, 'sqlite', 'sqlite', 10, seed,
                       native_runtime=None, evidence_dir=None, order='shuffle',
                       enable_prover=False) for seed in (0, 1)]
    _same_internal_pool(runs[0][0], runs[1][0])


def test_sqlwitness_shell_seed_changes_only_arrival_order():
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')
    runs = [run_sqlwitness(schema, None, QUERIES, 'sqlite', 'sqlite', 10, seed,
                           order='shuffle') for seed in (0, 1)]
    _same_internal_pool(runs[0][0], runs[1][0])
