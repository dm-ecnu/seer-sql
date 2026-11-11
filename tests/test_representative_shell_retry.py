import time

from seer import stream_runner
from seer2.schema import Schema


def test_representative_shell_retries_only_current_class_representative(monkeypatch):
    seen = []
    def no_witness(schema, dialect, a, b, budget, seed, call_id, session=None):
        seen.append((seed, call_id))
        time.sleep(0.003)
        return {'status': 'no_witness', 'native': {'verdict': 'unknown'}, 'database': None}
    monkeypatch.setattr(stream_runner, 'native_sqlwitness', no_witness)
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')
    _, result = stream_runner.run_sqlwitness(schema, None,
        [('a', 'SELECT x FROM t'), ('b', 'SELECT x FROM t')],
        'sqlite', 'sqlite', 0.04, 3, random_dbs=0, shell='representative')
    assert len(result['calls']) >= 2
    assert result['calls'][0]['round'] == 0
    assert result['calls'][1]['round'] == 1
    assert seen[0][0] == 0 and seen[1][0] == 1000
    assert {tuple((c['a'], c['b'])) for c in result['calls']} == {('b', 'a')}
