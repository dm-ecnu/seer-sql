from seer import stream_runner
from seer2.schema import Schema as NativeSchema
from seereval.schema import Schema as EvalSchema, Table


def test_sqlwitness_sqlite_uses_disposable_local_scratch(monkeypatch, tmp_path):
    from pathlib import Path
    from seereval import tools
    seen = []

    def fake_call(tool, request, hard_s, mem_gb, cpu_file, worker_cwd=None):
        assert tool == 'sqlwitness' and request['dialect'] == 'sqlite'
        assert Path(worker_cwd).is_dir()
        seen.append(worker_cwd)
        return {'verdict': 'eq', 'cpu': 0.0, 'cpu_complete': True, 'runq': 0.0}

    monkeypatch.setenv('SEER_SQLWITNESS_SQLITE_SCRATCH', str(tmp_path))
    monkeypatch.setattr(tools, '_call', fake_call)
    tools.call('sqlwitness', {'dialect': 'sqlite'}, 1)
    assert len(seen) == 1 and not Path(seen[0]).exists()


def test_released_pair_tools_share_request_and_native_replay_boundary(monkeypatch):
    from seereval import tools
    schema = EvalSchema('toy', [Table('t', [{'name': 'x', 'type': 'int'}])])
    seen = []
    def candidate(tool, request, hard_s):
        seen.append((tool, request, hard_s))
        return {'verdict': 'neq', 'instance': {'T': [{'X': 1}]}}
    monkeypatch.setattr(tools, 'call', candidate)
    for tool in ('parseval', 'xdatan', 'sqldriller_official'):
        outcome = stream_runner.native_pair_tool(tool, schema, 'sqlite',
            'SELECT x FROM t', 'SELECT 1 FROM t', 2.5, 0, 'call-0',
            stages=['testsuite'] if tool == 'sqldriller_official' else None)
        assert outcome['status'] == 'candidate'
        assert outcome['database'] == {'t': [{'x': 1}]}
        assert seen[-1][0] == tool and seen[-1][2] == 2.5
    assert seen[-1][1]['stages'] == ['testsuite']


def test_absent_released_witness_never_creates_an_equivalence_edge(monkeypatch):
    seen = []
    def absent(tool, *args, **kwargs):
        seen.append(tool)
        return {'status': 'no_witness', 'native': {'verdict': 'unknown'}, 'database': None}
    monkeypatch.setattr(stream_runner, 'native_pair_tool', absent)
    schema = NativeSchema('CREATE TABLE t(x INTEGER);', 'sqlite')
    _, result = stream_runner.run_sqlwitness(schema, None,
        [('a', 'SELECT x FROM t'), ('b', 'SELECT x FROM t')],
        'sqlite', 'sqlite', 0.2, 0, random_dbs=0, tool='parseval')
    assert seen and all(x == 'parseval' for x in seen)
    assert result['tool'] == 'parseval'
    assert result['pair_counts']['proved'] == result['pair_counts']['bounded'] == 0


def test_parseval_one_released_attempt_per_pair(monkeypatch):
    seen = []

    def no_counterexample(tool, *args, **kwargs):
        seen.append(tool)
        return {'status': 'no_witness', 'native': {'verdict': 'eq'}, 'database': None}

    monkeypatch.setattr(stream_runner, 'native_pair_tool', no_counterexample)
    schema = NativeSchema('CREATE TABLE t(x INTEGER);', 'sqlite')
    _, result = stream_runner.run_sqlwitness(schema, None,
        [('a', 'SELECT x FROM t'), ('b', 'SELECT x FROM t')],
        'sqlite', 'sqlite', 2, 0, random_dbs=0, tool='parseval')
    assert seen == ['parseval']
    assert result['metrics']['native_pair_calls'] == 1


def test_xdatan_multi_keeps_one_native_batch_and_replays_only_sigma_valid_database(monkeypatch):
    from seereval import tools
    schema = NativeSchema('CREATE TABLE t(x INTEGER CHECK (x > 0));', 'sqlite')
    eval_schema = EvalSchema('toy', [Table('t', [{'name': 'x', 'type': 'int'}])])
    requests = []
    monkeypatch.setenv("SEER_ENTRY_METRICS", "1")

    def native(tool, request, hard_s):
        requests.append(request)
        assert tool == 'xdatan' and len(request['mutants']) == 1
        assert request["_entry_metric_tool"] == "xdatan"
        assert request["_entry_metric_call_id"] == "batch-0"
        return {'verdict': 'neq', 'instances': [
            {'t': [{'x': -1}]}, {'t': [{'x': 1}]}],
            'states': ['Killed'], 'n_datasets': 2}

    monkeypatch.setattr(tools, 'call', native)
    _, result = stream_runner.run_xdatan_multi(
        schema, eval_schema,
        [('a', 'SELECT x FROM t'), ('b', 'SELECT x + 1 FROM t')],
        'sqlite', 'sqlite', 10, 0, random_dbs=0)
    assert len(requests) == 1
    assert result['metrics']['native_batch_calls'] == 1
    assert result['metrics']['native_pair_calls'] == 1
    assert result['calls'][0]['datasets'][0]['reason'] == 'sigma'
    assert result['calls'][0]['datasets'][1]['admitted'] is True
    assert result['pair_counts']['separated'] == 1
    assert result['metrics']['invalid_admitted_certificates'] == 0
