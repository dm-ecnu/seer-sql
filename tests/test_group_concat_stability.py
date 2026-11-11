from seer.stream import StreamPartition
from seer2.schema import Schema


def test_group_concat_having_is_not_a_stable_fingerprint_column():
    engine = StreamPartition(Schema('CREATE TABLE t(x TEXT);', 'sqlite'),
                             'sqlite', 'sqlite', budget_s=1, random_dbs=0)
    engine.add('q', "SELECT 1 AS g FROM t GROUP BY g HAVING GROUP_CONCAT(x) BETWEEN 'a' AND 'b'")
    observation = engine._run({'t': [{'x': 'a'}, {'x': 'b'}]}, ['q'])['q']
    assert not observation.stable
    assert observation.reason == 'insertion_order_sensitive'
    assert observation.cardinality is None
