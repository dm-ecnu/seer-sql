import time

from seer.stream_runner import run_sqlwitness, run_stream
from seer2.schema import Schema


def test_expired_budget_records_unprocessed_arrivals_without_comparing():
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')
    queries = [('a', 'SELECT x FROM t'), ('b', 'SELECT x FROM t WHERE x=1')]
    started = time.perf_counter() - 3
    stream, sr = run_stream(schema, queries, 'sqlite', 'sqlite', 1, 0,
                            native_runtime=None, evidence_dir=None,
                            enable_prover=False, start_time=started)
    witness, wr = run_sqlwitness(schema, None, queries, 'sqlite', 'sqlite', 1, 0,
                                 start_time=started)
    for engine, result in ((stream, sr), (witness, wr)):
        assert engine.arrival == []
        assert result['unprocessed_query_ids'] == ['a', 'b']
        assert result['metrics']['input_queries'] == 2
        assert result['metrics']['processed_queries'] == 0
        assert result['metrics']['unprocessed_queries'] == 2
        assert result['metrics']['throughput_queries_s'] == 0
