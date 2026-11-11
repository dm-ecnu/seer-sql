"""The paper-era panel must retain every official reference/candidate row."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import json
from collections import defaultdict
from pathlib import Path

from seereval.schema import Schema, to_ddl
from scripts.build_leetcode_verieql_paper_v1 import schema_from_official


ROOT = Path((SEER_ROOT))
SOURCE = ROOT / 'repro/verieql-original-input-v1/leetcode.jsonlines'
BATCHES = ROOT / 'datasets/batches/leetcode_verieql_paper_batches.jsonl'


def test_exact_paper_rows_and_native_schema():
    originals = defaultdict(list)
    for line in SOURCE.open():
        row = json.loads(line)
        originals[Path(row['file']).stem].append(row)
    batches = [json.loads(line) for line in BATCHES.open()]
    assert len(batches) == len(originals) == 56
    assert sum(map(len, originals.values())) == 23994
    for batch in batches:
        pid = batch['meta']['problem']
        rows = originals[pid]
        assert batch['meta']['original_pair_count'] == len(rows)
        assert batch['queries'][0]['sql'] == rows[0]['pair'][0]
        assert [(q['original_query_id'], q['sql']) for q in batch['queries'][1:]] == [
            (str(row['index']), row['pair'][1]) for row in rows]
        schema_file = BATCHES.parent / 'schemas/leetcode_verieql_paper' / f'{pid}.json'
        schema = json.loads(schema_file.read_text())
        native = schema['native']['verieql']
        assert native['schema'] == rows[0]['schema']
        assert native['constraint'] == rows[0]['constraint']
        assert native['contain_unsupported_constraints'] == rows[0]['contain_unsupported_constraints']
        if schema['unsupported']:
            assert batch['schema_path'] is None
            assert batch['meta']['unsupported_reason'] in (
                'unmapped_original_schema', 'unsupported_original_statement')
        elif batch['schema_path']:
            assert to_ddl(Schema.load(str(schema_file)), 'mysql', deferrable=False)


def test_official_foreign_and_comparison_constraints_exclude_nulls():
    row = {
        'schema': {'CHILD': {'ID': 'INT', 'PARENT_ID': 'INT', 'SCORE': 'INT'},
                   'PARENT': {'ID': 'INT', 'ALIAS': 'INT'}},
        'constraint': [
            {'foreign': [{'value': 'CHILD__PARENT_ID'}, {'value': 'PARENT__ID'}]},
            {'foreign': [{'value': 'CHILD__ID'}, {'value': 'PARENT__ALIAS'}]},
            {'primary': [{'value': 'PARENT__ID'}]},
            {'gt': [{'value': 'CHILD__SCORE'}, 0]},
        ],
    }
    schema = schema_from_official('x', row)
    child = next(t for t in schema['tables'] if t['name'] == 'CHILD')
    cols = {c['name']: c for c in child['columns']}
    assert all(cols[name]['not_null'] for name in ('ID', 'PARENT_ID', 'SCORE'))
    assert len(child['foreign_keys']) == 1
    assert schema['inclusions'] == [{
        'table': 'CHILD', 'columns': ['ID'],
        'ref_table': 'PARENT', 'ref_columns': ['ALIAS']}]
    assert {'table': 'CHILD', 'column': 'SCORE',
            'lo': -2147483648, 'hi': 2147483647} in schema['domains']
