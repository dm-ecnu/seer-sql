"""Freeze MySQL panel inputs for the shared in-process SQLite backend.

Uses SQLGlot 30.21.0 (MIT), https://sqlglot.com/; no handwritten SQL parser.
SQLite EXPLAIN rejects unsupported syntax/functions/column bindings before either
method runs. Identities and original budgets are retained even on rejection.
"""
import argparse
import copy
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'eval')]
import sqlglot
from sqlglot.errors import ErrorLevel
from seereval.schema import Schema, to_ddl

VERSION = '30.21.0'

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def convert(sql, dialect='mysql'):
    statements = sqlglot.transpile(sql, read=dialect, write='sqlite',
                                   error_level=ErrorLevel.RAISE,
                                   unsupported_level=ErrorLevel.RAISE)
    if len(statements) != 1 or not statements[0]:
        raise ValueError('expected one SQL statement')
    return statements[0]

def prepare(source, out):
    assert sqlglot.__version__ == VERSION, sqlglot.__version__
    source, out = Path(source).resolve(), Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    counts = dict(batches=0, queries=0, reference_pairs=0, unsupported_queries=0,
                  unsupported_reference_pairs=0, unsupported_batches=0)
    for line in source.open():
        original = json.loads(line)
        b = copy.deepcopy(original)
        assert b['dialect'] == 'mysql', 'only the three authorized MySQL panels'
        assert b['dataset'] in ('leetcode_verieql_original_batches',
            'calcite_verieql_paper_original', 'literature_verieql_paper_original')
        m = b.setdefault('meta', {})
        info = dict(rules_sha256=sha(__file__), sqlglot_version=VERSION,
                    sqlite_version=sqlite3.sqlite_version,
                    source=str(source), source_sha256=sha(source),
                    source_row_sha256=hashlib.sha256(line.encode()).hexdigest(),
                    original_dialect=b['dialect'], original_query_count=len(b['queries']),
                    original_reference_pairs=len(b['queries'])-1,
                    unsupported_qids=[], unsupported_reasons={},
                    data_conversion='no source rows; generated witnesses use SQLite native values')
        b['dialect'] = 'sqlite'
        reason = None
        con = sqlite3.connect(':memory:')
        try:
            sp = (source.parent/b['schema_path']).resolve()
            data = json.loads(sp.read_text())
            es = Schema.load(sp)
            ddls = [convert(ddl) for ddl in to_ddl(es, 'mysql', deferrable=False)]
            # Keep the declared logical types/PK/FK/UNIQUE contract. SQL expressions
            # inside checks use the same strict dialect translator as the queries.
            for ck in data.get('checks', []):
                ck['expr'] = convert('SELECT '+ck['expr'])[7:]
            data.setdefault('native', {})['sqlite_panel'] = {'ddl': ddls}
            dest = out.parent/'schemas'/f'{sha(sp)}.json'
            dest.parent.mkdir(exist_ok=True)
            content = json.dumps(data, sort_keys=True, indent=2)+'\n'
            if not dest.exists() or dest.read_text() != content:
                dest.write_text(content)
            info.update(schema_source=str(sp), schema_source_sha256=sha(sp),
                        schema_sha256=sha(dest), ddl_sha256=hashlib.sha256('\n'.join(ddls).encode()).hexdigest())
            b['schema_path'] = str(dest.relative_to(out.parent))
            for ddl in ddls:
                con.execute(ddl)
        except Exception as exc:
            reason = 'schema: '+type(exc).__name__+': '+str(exc)
        for q in b['queries']:
            try:
                if reason:
                    raise ValueError(reason)
                q['sql'] = convert(q['sql'])
                con.execute('EXPLAIN '+q['sql']).fetchall()
            except Exception as exc:
                info['unsupported_qids'].append(q['qid'])
                info['unsupported_reasons'][q['qid']] = type(exc).__name__+': '+str(exc)
        con.close()
        refs = [q for q in b['queries'] if q.get('role')=='reference']
        assert len(refs)==1
        if refs[0]['qid'] in info['unsupported_qids']:
            for q in b['queries']:
                if q['qid'] not in info['unsupported_qids']:
                    info['unsupported_qids'].append(q['qid'])
                    info['unsupported_reasons'][q['qid']] = 'reference_query_unsupported'
        info['unsupported_reference_pairs'] = sum(q['qid'] in info['unsupported_qids'] for q in b['queries'] if q.get('role')=='candidate')
        m['sqlite_conversion'] = info
        rows.append(b)
        counts['batches'] += 1; counts['queries'] += len(b['queries'])
        counts['reference_pairs'] += len(b['queries'])-1
        counts['unsupported_queries'] += len(info['unsupported_qids'])
        counts['unsupported_reference_pairs'] += info['unsupported_reference_pairs']
        counts['unsupported_batches'] += bool(info['unsupported_qids'])
    out.write_text(''.join(json.dumps(b, sort_keys=True)+'\n' for b in rows))
    print(json.dumps(dict(output=str(out), output_sha256=sha(out), rules_sha256=sha(__file__), **counts)), flush=True)

if __name__ == '__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--source',required=True);ap.add_argument('--out',required=True)
    a=ap.parse_args();prepare(a.source,a.out)
