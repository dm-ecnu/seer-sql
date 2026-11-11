import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import csv
from pathlib import Path

from seer.official_batch import mysql_connection_failure


def test_released_driver_connection_loss_is_infrastructure_failure():
    root = Path((SEER_ROOT + '/repro/stream-partition-v1/leetcode-proof-curve-v1'))
    source = (root / 'seer_stream_sqlwitness/s2/leetcode-1364-n500/'
              'sqlwitness-microbatches/round-002/official-batch.csv')
    rows = list(csv.DictReader(source.open()))
    assert any(mysql_connection_failure(row) for row in rows)
    assert sum(mysql_connection_failure(row) for row in rows) == 27
    assert not mysql_connection_failure({'Counterexample': 'COUNTEREXAMPLE_FOUND', 'Error': ''})
    assert not mysql_connection_failure({'Counterexample': "{'t': [['x'], [1]]}", 'Error': ''})
