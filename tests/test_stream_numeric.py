import datetime as dt

from seer.front import Front
from seer.stream import StreamPartition
from seer.stream_numeric import bounded_numeric_admission
from seer2.schema import Schema


def admit(ddl, dialect, left, right):
    schema = Schema(ddl, dialect)
    front = Front(schema, dialect)
    return bounded_numeric_admission(front.translate(left), front.translate(right), dialect, schema, 2)


def test_exact_avg_lineage_is_admitted_on_supported_engines():
    for dialect in ('postgres', 'mysql'):
        for typ in ('INTEGER', 'DECIMAL(10,2)'):
            result = admit(f'CREATE TABLE t(x {typ});', dialect,
                           'SELECT AVG(x) FROM t', 'SELECT AVG(x) FROM t WHERE 1=1')
            assert result['admitted'] and result['profile'] == 'direct_exact_avg_v1'


def test_binary_float_and_rounded_avg_usage_are_rejected():
    for dialect in ('postgres', 'mysql'):
        assert not admit('CREATE TABLE t(x DOUBLE);', dialect,
                         'SELECT AVG(x) FROM t', 'SELECT AVG(x) FROM t')['admitted']
        assert not admit('CREATE TABLE t(x INTEGER);', dialect,
                         'SELECT AVG(x) FROM t HAVING AVG(x)>0',
                         'SELECT AVG(x) FROM t HAVING AVG(x)>0')['admitted']
        assert not admit('CREATE TABLE t(x INTEGER);', dialect,
                         'SELECT AVG(x)+1 FROM t', 'SELECT AVG(x)+1 FROM t')['admitted']


def test_different_exact_avg_source_does_not_share_rounding_profile():
    result = admit('CREATE TABLE t(x DECIMAL(10,2), y DECIMAL(12,2));', 'postgres',
                   'SELECT AVG(x) FROM t', 'SELECT AVG(y) FROM t')
    assert not result['admitted']
    assert result['reason'] == 'numeric_output_lineage_mismatch'
    wide = admit('CREATE TABLE t(x DECIMAL(65,2));', 'mysql',
                 'SELECT AVG(x) FROM t', 'SELECT AVG(x) FROM t')
    assert not wide['admitted'] and wide['reason'] == 'mysql_numeric_sum_precision'


def test_sqlite_avg_remains_unadmitted_and_dates_use_real_domain_days():
    assert not admit('CREATE TABLE t(x INTEGER);', 'sqlite',
                     'SELECT AVG(x) FROM t', 'SELECT AVG(x) FROM t')['admitted']
    s = StreamPartition(Schema('CREATE TABLE t(d DATE);', 'sqlite'),
                        'sqlite', 'sqlite', budget_s=0, random_dbs=0)
    inside = dt.date(2000, 1, 1)
    s.search.domain.dlo = (inside - dt.date(1970, 1, 1)).days - 1
    s.search.domain.dhi = s.search.domain.dlo + 2
    assert s._domain_covers({'t': [{'d': inside}]})
    assert not s._domain_covers({'t': [{'d': dt.date(1900, 1, 1)}]})
