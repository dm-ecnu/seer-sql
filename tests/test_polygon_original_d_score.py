"""The original D task ends at its first certified disambiguating database."""
import json

import pytest

from scripts.score_polygon_original_d_v1 import DATA, RUN, EXPECTED, score_one, score_official


def test_first_split_is_task_time_but_full_partition_wall_is_retained(tmp_path):
    batch = json.loads(next((DATA / 'cubes_d50.jsonl').open()))
    status = next(json.loads(line) for line in (RUN / 'status.jsonl').open()
                  if json.loads(line)['batch'] == batch['batch_id'])
    output = RUN / 'd50' / batch['batch_id']
    scored = score_one(batch, status, output)
    assert scored['solved']
    assert 0 < scored['time_to_solved_s'] < scored['full_partition_wall_s']
    assert scored['invalid_admitted_certificates'] == 0
    with pytest.raises(ValueError, match='classification differs'):
        score_one(batch, {**status, 'solved': False}, output)
    (tmp_path / 'config.json').write_bytes((output / 'config.json').read_bytes())
    result = json.loads((output / 'result.json').read_text())
    result['witnesses'][0]['active'] = False
    (tmp_path / 'result.json').write_text(json.dumps(result))
    with pytest.raises(ValueError, match='disabled database'):
        score_one(batch, status, tmp_path)


def test_official_csv_is_aligned_by_original_task_id(tmp_path, monkeypatch):
    monkeypatch.setitem(EXPECTED, 50, 2)
    batches = {
        'a': {'meta': {'polygon_id': 0, 'benchmark': 'a.txt'}},
        'b': {'meta': {'polygon_id': 1, 'benchmark': 'b.txt'}},
    }
    csv_file = tmp_path / 'official.csv'
    csv_file.write_text('ID,Benchmark,?Solved,Time\n'
                        '0,a.txt,True,3.5\n1,b.txt,False,60\n')
    result = score_official(50, batches, csv_file)
    assert result['solved'] == 1
    assert result['median_solved_time_s'] == 3.5
    csv_file.write_text('ID,Benchmark,?Solved,Time\n'
                        '0,b.txt,True,3.5\n1,b.txt,False,60\n')
    with pytest.raises(ValueError, match='benchmark differs'):
        score_official(50, batches, csv_file)
