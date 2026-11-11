import pytest
from pymysql.err import OperationalError, ProgrammingError

from seer.stream import observe
from seer2.backends import EngineRejected, QueryError, _MyInst
from seer2.refine import Prepared


class FakeCursor:
    def __init__(self, error):
        self.error = error

    def execute(self, sql):
        raise self.error


class FakeConnection:
    def __init__(self, error):
        self.error = error

    def cursor(self):
        return FakeCursor(self.error)


def test_mysql_lost_connection_aborts_observation_instead_of_becoming_sql_value():
    inst = _MyInst(FakeConnection(OperationalError(2013, 'Lost connection to MySQL server during query')),
                   'seer2_10_1', 10)
    with pytest.raises(EngineRejected, match=r'connection failure \(2013\)'):
        observe(inst, Prepared('SELECT 1', 'mysql', 'mysql'), lambda: None)


def test_mysql_scratch_database_name_is_not_part_of_sql_error_value():
    outputs = []
    for db in ('seer2_10_1', 'seer2_20_2'):
        inst = _MyInst(FakeConnection(ProgrammingError(1146, f"Table '{db}.missing' doesn't exist")),
                       db, 10)
        output = observe(inst, Prepared('SELECT * FROM missing', 'mysql', 'mysql'), lambda: None)
        assert output.stable
        outputs.append(output.key)
    assert outputs[0] == outputs[1]


def test_generic_connection_failure_cannot_define_a_stable_fingerprint():
    class FakeInst:
        def run(self, sql):
            return QueryError('OperationalError: (2013, Lost connection to MySQL server during query)'), 0

    output = observe(FakeInst(), Prepared('SELECT 1', 'mysql', 'mysql'), lambda: None)
    assert not output.stable
    assert output.cardinality is None
