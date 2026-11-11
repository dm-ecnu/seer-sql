from seer.stream import StreamPartition
from seer2.schema import Schema


def test_arrival_and_pool_events_record_full_coverage_curve():
    engine = StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
                             'sqlite', 'sqlite', budget_s=10, random_dbs=0)
    engine.add('a', 'SELECT 1')
    engine.add('b', 'SELECT 2')
    points = [e for e in engine.events if e['event'] in ('arrival', 'pool_added')]
    assert [e['query'] for e in points if e['event']=='arrival'] == ['a', 'b']
    assert points[-1]['processed_queries'] == 2
    assert points[-1]['total_pairs'] == 1
    assert points[-1]['separated_pairs'] == 1
    assert all(0 <= e['separated_pairs'] <= e['total_pairs'] for e in points)
    assert all('wall_s' in e and 'class_count' in e for e in points)
