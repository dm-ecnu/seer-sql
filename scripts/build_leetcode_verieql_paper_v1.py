"""Convert the VeriEQL paper-era JSONL into exact-query SEER problem batches.

Keep the official row in the batch metadata by digest, and refuse constraint
forms that cannot be represented by the shared SQL schema.
"""
from __future__ import annotations

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import hashlib
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path((SEER_ROOT))
SOURCE = ROOT / 'repro/verieql-original-input-v1/leetcode.jsonlines'
DEST = ROOT / 'datasets/batches/leetcode_verieql_paper_batches.jsonl'
SCHEMA_DIR = ROOT / 'datasets/batches/schemas/leetcode_verieql_paper'


def colref(value):
    assert isinstance(value, dict) and set(value) == {'value'}
    table, column = value['value'].split('__', 1)
    return table, column


def literal(value):
    if isinstance(value, dict):
        if 'literal' in value:
            return value['literal']
        if 'date' in value:
            return value['date']
    return value


def sql_literal(value):
    value = literal(value)
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def schema_from_official(pid, row):
    raw = row['schema']
    tables = []
    for table, columns in raw.items():
        out_cols = []
        for name, typ in columns.items():
            typ = str(typ).upper()
            norm = ('int' if typ == 'INT' else
                    'numeric' if typ == 'NUMERIC' else
                    'date' if typ == 'DATE' else
                    'time' if typ == 'TIME' else
                    'boolean' if typ == 'BOOL' else 'varchar')
            out_cols.append({'name': name, 'type': norm, 'not_null': False})
        tables.append({'name': table, 'columns': out_cols, 'primary_key': [],
                       'unique': [], 'foreign_keys': []})
    by_name = {t['name']: t for t in tables}
    out = {'name': pid, 'tables': tables, 'source': 'VeriEQL paper-era original JSONL',
           'checks': [], 'inclusions': [], 'enums': [], 'domains': [],
           'unsupported': [], 'native': {'verieql': {
               'schema': raw, 'constraint': row.get('constraint'),
               'contain_unsupported_constraints': row.get('contain_unsupported_constraints', False)}}}
    for table, columns in raw.items():
        for name, typ in columns.items():
            if typ is None:
                # Guessing a type would change the solver/DBMS domain.
                out['unsupported'].append(f'undefined source type: {table}.{name}')
            elif str(typ).upper() == 'INT':
                # VeriEQL's create_database constrains INT to signed 32-bit;
                # the shared MySQL DDL uses BIGINT, so retain that bound.
                out['domains'].append({'table': table, 'column': name,
                                       'lo': -2147483648, 'hi': 2147483647})
            elif str(typ).upper() in ('BOOL', 'BOOLEAN'):
                # Unlike the explicit `in` constraint, a type restriction
                # still permits SQL NULL.
                out['enums'].append({'table': table, 'column': name,
                                     'values': [0, 1]})
    constraints = [] if row.get('contain_unsupported_constraints') else row.get('constraint') or []
    single_column_keys = {
        tuple(colref(item) for item in c['primary'])[0]
        for c in constraints if 'primary' in c and len(c['primary']) == 1
    }
    for constraint in constraints:
        if len(constraint) != 1:
            out['unsupported'].append(repr(constraint))
            continue
        op, args = next(iter(constraint.items()))
        try:
            if op == 'primary':
                refs = [colref(x) for x in args]
                if len({t for t, _ in refs}) != 1:
                    raise ValueError('cross-table key')
                table = by_name[refs[0][0]]
                names = [c for _, c in refs]
                if table['primary_key']:
                    table['unique'].append(names)
                else:
                    table['primary_key'] = names
                for name in names:
                    next(c for c in table['columns'] if c['name'] == name)['not_null'] = True
            elif op == 'foreign':
                (table, name), (parent, parent_name) = [colref(x) for x in args]
                # VeriEQL requires an actual matching RHS value; a NULL LHS
                # cannot satisfy its equality. SQL MATCH SIMPLE would permit
                # that NULL, so make the source column non-null explicitly.
                next(c for c in by_name[table]['columns'] if c['name'] == name)['not_null'] = True
                if (parent, parent_name) in single_column_keys:
                    by_name[table]['foreign_keys'].append(
                        {'columns': [name], 'ref_table': parent,
                         'ref_columns': [parent_name]})
                else:
                    out['inclusions'].append({'table': table, 'columns': [name],
                                              'ref_table': parent, 'ref_columns': [parent_name]})
            elif op == 'in':
                table, name = colref(args[0])
                values = [literal(x) for x in args[1]]
                out['enums'].append({'table': table, 'column': name, 'values': values})
                next(c for c in by_name[table]['columns'] if c['name'] == name)['not_null'] = True
            elif op == 'between':
                table, name = colref(args[0])
                out['domains'].append({'table': table, 'column': name,
                                       'lo': literal(args[1]), 'hi': literal(args[2])})
                next(c for c in by_name[table]['columns'] if c['name'] == name)['not_null'] = True
            elif op in ('gt', 'gte', 'lt', 'lte', 'neq'):
                table, name = colref(args[0])
                symbol = {'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<=', 'neq': '<>'}[op]
                if isinstance(args[1], dict) and 'value' in args[1]:
                    other_table, other_name = colref(args[1])
                    if other_table != table:
                        raise ValueError('cross-table comparison')
                    rhs = '`' + other_name.replace('`', '``') + '`'
                    next(c for c in by_name[table]['columns'] if c['name'] == other_name)['not_null'] = True
                else:
                    rhs = sql_literal(args[1])
                next(c for c in by_name[table]['columns'] if c['name'] == name)['not_null'] = True
                if op == 'neq' and rhs == 'NULL':
                    next(c for c in by_name[table]['columns'] if c['name'] == name)['not_null'] = True
                else:
                    out['checks'].append({'table': table,
                                          'expr': '`' + name.replace('`', '``') + '` ' + symbol + ' ' + rhs})
            else:
                out['unsupported'].append(repr(constraint))
        except (KeyError, StopIteration, TypeError, ValueError, IndexError) as exc:
            out['unsupported'].append(f'{constraint!r}: {exc}')
    return out


def main():
    rows = [json.loads(line) for line in SOURCE.open()]
    assert len(rows) == 23994
    source_hash = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    grouped = defaultdict(list)
    for row in rows:
        pid = Path(row['file']).stem
        grouped[pid].append(row)
    assert len(grouped) == 56
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    batches = []
    for pid, group in sorted(grouped.items(), key=lambda x: int(x[0])):
        reference = group[0]['pair'][0]
        schema = group[0]['schema']
        constraint = group[0].get('constraint')
        assert all(x['pair'][0] == reference and x['schema'] == schema and
                   x.get('constraint') == constraint for x in group)
        normalized = schema_from_official(pid, group[0])
        schema_path = SCHEMA_DIR / f'{pid}.json'
        schema_path.write_text(json.dumps(normalized, indent=2) + '\n')
        # The original 627 workload consists of UPDATE statements, outside
        # the stream's SELECT-query contract. Preserve all pairs explicitly.
        unsupported = ('unsupported_original_statement' if
                       not reference.lstrip().upper().startswith(('SELECT', 'WITH'))
                       else 'unmapped_original_schema' if normalized['unsupported']
                       else None)
        queries = [{'qid': 'gold', 'role': 'reference', 'sql': reference}]
        queries += [{'qid': f'c{i}', 'role': 'candidate', 'sql': row['pair'][1],
                     'original_query_id': str(row['index'])}
                    for i, row in enumerate(group)]
        batches.append({'batch_id': f'leetcode-verieql-{pid}',
                        'dataset': 'leetcode_verieql_original_batches',
                        'dialect': 'mysql',
                        'schema_path': None if unsupported else f'schemas/leetcode_verieql_paper/{pid}.json',
                        'queries': queries,
                        'meta': {'problem': pid, 'original_pair_count': len(group),
                                 'source': 'VeriEQL 003f5361b4a6 benchmarks/leetcode/leetcode.jsonlines',
                                 'source_sha256': source_hash,
                                 'source_first_line': rows.index(group[0]) + 1,
                                 'unsupported_reason': unsupported,
                                 'mapped_unsupported_constraints': normalized['unsupported'],
                                 'official_ignores_constraints': bool(group[0].get('contain_unsupported_constraints'))}})
    tmp = DEST.with_suffix('.jsonl.tmp')
    with tmp.open('w') as sink:
        for batch in batches:
            sink.write(json.dumps(batch, separators=(',', ':')) + '\n')
    tmp.replace(DEST)
    print(json.dumps({'batches': len(batches), 'pairs': sum(b['meta']['original_pair_count'] for b in batches),
                      'mapped_unsupported_problems': [b['meta']['problem'] for b in batches if b['meta']['mapped_unsupported_constraints']],
                      'unsupported_problems': {b['meta']['problem']: b['meta']['unsupported_reason']
                                               for b in batches if b['meta']['unsupported_reason']},
                      'destination': str(DEST)}))


if __name__ == '__main__':
    main()
