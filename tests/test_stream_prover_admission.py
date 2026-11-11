import pytest
from seer.stream_prover import semantics_profile, PROFILE
from seer2.schema import Schema


def test_safe_relational_count_profile_and_unsafe_features():
    s = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')
    assert semantics_profile(s, ['SELECT x FROM t', 'SELECT x FROM t WHERE 1=1'], 'sqlite', 'sqlite') == PROFILE
    assert semantics_profile(s, ['SELECT COUNT(x) FROM t'], 'sqlite', 'sqlite') == PROFILE
    assert semantics_profile(s, ['SELECT x, COUNT(x) FROM t'], 'sqlite', 'sqlite') is None
    for q in ['SELECT x+1 FROM t', 'SELECT x FROM t ORDER BY x',
              'SELECT x FROM t LIMIT 1', 'SELECT RANDOM() FROM t',
              "SELECT x FROM t WHERE x='1'", 'SELECT AVG(x) FROM t']:
        assert semantics_profile(s, [q], 'sqlite', 'sqlite') is None
    assert semantics_profile(s, ['SELECT x FROM t'], 'sqlite', 'postgres') is None
    strings = Schema('CREATE TABLE t(x TEXT);', 'sqlite')
    assert semantics_profile(strings, ['SELECT x FROM t'], 'sqlite', 'sqlite') is None
