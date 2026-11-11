"""Input/output adapter for the released SQLWitness experiment driver.

The driver and its benchmark loader remain byte-identical to the author artifact.
Each stream batch is materialized as one of their supported benchmark layouts;
the native driver then handles the whole set of query pairs in one process.
"""
from __future__ import annotations

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import ast
import csv
import datetime
import io
import json
import itertools
import os
import re
import signal
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from seereval.engine import normalize_sql
from seereval.schema import to_sqlwitness
from seereval.tools import sqlite_dq_literals

ARTIFACT = Path((SEER_ROOT + '/datasets/raw/sqlwitness/x/SQLWitness-artifact'))
_SQL_PARTS = re.compile(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`)")


def mysql_connection_failure(row: dict) -> bool:
    """The released driver writes connection loss into a normal CSV row."""
    text = ' '.join(str(row.get(key) or '') for key in ('Counterexample', 'Error')).lower()
    return any(marker in text for marker in (
        '(2013,', '(2006,', '(2003,', '(2055,', '(1040,',
        'lost connection to mysql server', 'server has gone away',
        'too many connections'))


def _line_sql(sql: str) -> str:
    parts = _SQL_PARTS.split(sql)
    if any('\n' in p or '\r' in p for p in parts[1::2]):
        raise ValueError('quoted newline cannot be encoded by the official paired-query loader')
    return ''.join(p if i % 2 else re.sub(r'\s+', ' ', p) for i, p in enumerate(parts)).strip()


def _csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def materialize(root: Path, eval_schema, dialect: str, pairs: list[tuple[str, str, str, str]]) -> tuple[str, dict[tuple[str, str], tuple[str, str]]]:
    """Return (official benchmark name, CSV key -> query-id pair).

    `pairs` contains (left id, right id, left SQL, right SQL) in requested
    native order. The output directory is unique to one immutable batch run.
    """
    root = Path(root)
    experiments = root / 'experiments'
    experiments.mkdir(parents=True, exist_ok=False)
    for name in ('run_sqlwitness.py', 'benchmarks.py'):
        (experiments / name).symlink_to(ARTIFACT / 'experiments' / name)
    tables, constraints = to_sqlwitness(eval_schema)
    (root / 'benchmarks').mkdir()
    if dialect == 'sqlite':
        benchmark, folder = 'bird', 'bird'
    elif dialect == 'mysql' and 'constraints_file' in eval_schema.native.get('sqlwitness', {}):
        benchmark, folder = 'leetcode', 'leetcode'
    elif dialect == 'mysql':
        benchmark, folder = 'minidev-mysql', 'minidev'
    elif dialect in ('postgres', 'postgresql'):
        benchmark, folder = 'minidev-postgresql', 'minidev'
    else:
        raise ValueError(f'unsupported SQLWitness dialect {dialect}')
    base = root / 'benchmarks' / folder
    base.mkdir()
    prepared = []
    for a, b, qa, qb in pairs:
        if dialect == 'sqlite':
            qa, qb = sqlite_dq_literals(qa, eval_schema), sqlite_dq_literals(qb, eval_schema)
        prepared.append((a, b, normalize_sql(qa, dialect), normalize_sql(qb, dialect)))
    mapping: dict[tuple[str, str], tuple[str, str]] = {}
    if benchmark == 'leetcode':
        schemas = base / 'schemas'
        cons_dir = base / 'constraints'
        gts = base / 'groundtruths'
        cands = base / 'candidates'
        for d in (schemas, cons_dir, gts, cands):
            d.mkdir()
        shared_schema = root / 'schema.json'
        shared_schema.write_text(json.dumps({'Tables': tables}), encoding='utf-8')
        shared_constraints = root / 'constraints.yml'
        shared_constraints.write_text(constraints, encoding='utf-8')
        groups = defaultdict(list)
        for a, b, qa, qb in prepared:
            groups[a].append((b, qa, qb))
        for i, (a, items) in enumerate(groups.items()):
            pid = f'p{i:06d}'
            (schemas / f'{pid}.json').symlink_to(shared_schema)
            (cons_dir / f'{pid}.yml').symlink_to(shared_constraints)
            _csv(gts / f'{pid}.csv', ['mysql_groundtruth'], [[items[0][1]]])
            _csv(cands / f'{pid}.csv', ['qid', 'mysql_query'], [[j, qb] for j, (_, _, qb) in enumerate(items)])
            for j, (b, _, _) in enumerate(items):
                mapping[(pid, str(j))] = (a, b)
    else:
        schema_dir = base / ('schemas' if benchmark == 'bird' else
                             'mysql_schemas' if benchmark == 'minidev-mysql' else 'postgresql_schemas')
        cons_dir = base / ('constraints' if benchmark == 'bird' else
                           'mysql_constraints' if benchmark == 'minidev-mysql' else 'postgresql_constraints')
        schema_dir.mkdir()
        cons_dir.mkdir()
        (schema_dir / 'batch.json').write_text(json.dumps({'Tables': tables}), encoding='utf-8')
        # Reuse the m36 frozen adapter (a94fac0f): the released parser
        # accepts LeetCode YAML through a .yml path, not raw YAML text.
        paired_constraints = constraints
        if dialect == 'sqlite' and re.match(r'^\s*(row_dep|col_dep|col_val)\s*:', constraints, re.M):
            source_constraints = (root / 'source-constraints.yml').resolve()
            source_constraints.write_text(constraints, encoding='utf-8')
            paired_constraints = str(source_constraints)
        (cons_dir / 'batch').write_text(paired_constraints, encoding='utf-8')
        delim = '\t'
        lines = [_line_sql(qa) + delim + 'batch\n' for _, _, qa, _ in prepared]
        gt = 'dev.sql' if benchmark == 'bird' else (
            'mini_dev_mysql_gold.sql' if benchmark == 'minidev-mysql' else 'mini_dev_postgresql_gold.sql')
        cand = 'omni_maj.json' if benchmark == 'bird' else (
            'predict_mini_dev_gpt-5.4_cot_MySQL.json' if benchmark == 'minidev-mysql'
            else 'predict_mini_dev_gpt-5.4_cot_PostgreSQL.json')
        (base / gt).write_text(''.join(lines), encoding='utf-8')
        (base / cand).write_text(json.dumps([_line_sql(qb) for _, _, _, qb in prepared]), encoding='utf-8')
        for i, (a, b, _, _) in enumerate(prepared, 1):
            mapping[(str(i), 'batch')] = (a, b)
    (root / 'pair-map.json').write_text(json.dumps({f'{a}|{b}': list(v) for (a, b), v in mapping.items()}), encoding='utf-8')
    return benchmark, mapping


def _safe_value(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        values = [_safe_value(x) for x in node.elts]
        return values if isinstance(node, ast.List) else tuple(values)
    if isinstance(node, ast.Dict):
        return {_safe_value(k): _safe_value(v) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _safe_value(node.operand)
        if isinstance(value, (int, float)):
            return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        if node.func.value.id == 'datetime' and node.func.attr in ('date', 'datetime', 'time') and not node.keywords:
            return getattr(datetime, node.func.attr)(*[_safe_value(x) for x in node.args])
    raise ValueError(f'unsupported counterexample literal: {type(node).__name__}')


def parse_counterexample(value: str) -> dict:
    data = _safe_value(ast.parse(value, mode='eval').body)
    if not isinstance(data, dict):
        raise ValueError('counterexample is not a table dictionary')
    return {str(table): [dict(zip(rows[0], row)) for row in rows[1:]]
            for table, rows in data.items() if isinstance(rows, list) and rows}


class _CsvTail:
    """Yield only complete CSV records, including records with quoted newlines."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.pending = b''
        self.scan = 0
        self.quoted = False

    def read(self):
        if not self.path.exists():
            return []
        with self.path.open('rb') as handle:
            handle.seek(self.offset)
            self.pending += handle.read()
            self.offset = handle.tell()
        rows = []
        while self.scan < len(self.pending):
            byte = self.pending[self.scan]
            if byte == 34:
                self.quoted = not self.quoted
            elif byte == 10 and not self.quoted:
                raw = self.pending[:self.scan + 1]
                record = list(csv.reader(io.StringIO(raw.decode('utf-8'))))
                if len(record) != 1:
                    raise ValueError('official CSV record did not parse as one row')
                rows.append(record[0])
                self.pending = self.pending[self.scan + 1:]
                self.scan = 0
                continue
            self.scan += 1
        return rows


def run_official_batch(schema, eval_schema, queries, dialect, backend, budget_s,
                       seed, order, start_time, output_dir, method_seed=0):
    """Run the released experiment driver once over all eligible pairs in one process.

    CSV rows are consumed as the driver flushes them, so each candidate still
    goes through the existing Sigma and fresh-DBMS admission before B expires.
    """
    import random

    from seereval.engine import _coerce
    from .stream import StreamPartition

    sqls = list(queries)
    if order == 'reverse':
        sqls.reverse()
    elif order == 'shuffle':
        random.Random(seed).shuffle(sqls)
    elif order != 'source':
        raise ValueError('unknown arrival order')
    engine = StreamPartition(schema, dialect, backend, budget_s=budget_s,
                             seed=method_seed, start_time=start_time)
    for q, sql in sqls:
        if engine.remaining() <= 0 or not engine.add(q, sql, compare=False):
            break
    targets = [(a, b, engine.queries[a], engine.queries[b])
               for a, b in itertools.combinations(engine.arrival, 2)
               if engine.fingerprints[a] == engine.fingerprints[b]]
    calls = []
    driver_status = 'not_started'
    if targets and engine.remaining() > 0:
        shadow = Path(output_dir) / 'official-input'
        bench, mapping = materialize(shadow, eval_schema,
                                      'postgresql' if dialect == 'postgres' else dialect, targets)
        (shadow / 'sitecustomize.py').write_text(
            f'import random\nrandom.seed({int(method_seed)})\n' + (
                'from sqlwitness.online.runner import DatabaseRunner\n'
                '_init = DatabaseRunner.__init__\n'
                'def _memory_init(self, *args, **kwargs):\n'
                '    _init(self, *args, **kwargs)\n'
                '    if self.dialect_name == \"sqlite\": self.db_file = \":memory:\"\n'
                'DatabaseRunner.__init__ = _memory_init\n'
                if dialect == 'sqlite' else ''), encoding='utf-8')
        scratch = Path(output_dir) / 'official-sqlite-scratch'
        scratch.mkdir()
        output = Path(output_dir) / 'official-batch.csv'
        log = Path(output_dir) / 'official-batch.log'
        # A private mount keeps sqlite:///test.db under /data while avoiding
        # shared-disk I/O. The artifact Python files and arguments are unchanged.
        command = ['unshare', '-Urm', '--', 'sh', '-c',
                   'mount -t tmpfs -o size=2g tmpfs "$1" && cd "$1" && exec "$2" "$3" --benchmark "$4" --output "$5"',
                   'sh', str(scratch), (SEER_ROOT + '/repro/sqlwitness/venv-sw-py311-pinned/bin/python'),
                   str(shadow / 'experiments/run_sqlwitness.py'), bench, str(output)]
        if dialect == 'sqlite':
            command = [command[-4], command[-3], '--benchmark', bench, '--output', str(output)]
        tail = _CsvTail(output)
        header = None
        native_start = time.perf_counter()
        validation_s = 0.0
        child_env = os.environ.copy()
        child_env['PYTHONPATH'] = str(shadow) + os.pathsep + child_env.get('PYTHONPATH', '')
        child_env['PYTHONHASHSEED'] = str(int(method_seed))
        with log.open('w') as log_handle:
            proc = subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT,
                                    start_new_session=True, env=child_env)
            try:
                while True:
                    for values in tail.read():
                        if header is None:
                            header = values
                            continue
                        row = dict(zip(header, values))
                        if mysql_connection_failure(row):
                            raise RuntimeError('official SQLWitness MySQL connection failure; see ' + str(output))
                        key = ((row['ProblemID'], row['QueryID']) if bench == 'leetcode'
                               else (row['Index'], row['Schema']))
                        a, b = mapping[key]
                        native = {'verdict': 'neq' if row.get('Refuted') == 'True' else 'eq',
                                  'secs': float(row['Time']) if row.get('Time') else None,
                                  'iterations': row.get('Iterations') or None,
                                  'error': row.get('Error') or None}
                        if native['verdict'] == 'eq' and native['secs'] is not None and native['secs'] >= 58.8:
                            native['verdict'] = 'timeout'
                        if native['error'] or str(row.get('Counterexample', '')).startswith('ERROR:'):
                            native['verdict'] = 'error'
                        rec = {'a': a, 'b': b, 'round': 0, 'native': native,
                               'wall_s': time.perf_counter() - engine.started,
                               'official_csv_index': len(calls) + 1}
                        calls.append(rec)
                        if native['verdict'] == 'neq':
                            candidate = row.get('Counterexample') or ''
                            if candidate and candidate != 'COUNTEREXAMPLE_FOUND':
                                t = time.perf_counter()
                                try:
                                    original = parse_counterexample(candidate)
                                    database = {}
                                    for table in eval_schema.tables:
                                        rows = next((r for name, r in original.items()
                                                     if name.lower() == table.name.lower()), [])
                                        database[table.name.lower()] = []
                                        for values in rows:
                                            low = {str(k).lower(): v for k, v in values.items()}
                                            database[table.name.lower()].append({
                                                c['name'].lower(): _coerce(low.get(c['name'].lower()), c['type'])
                                                for c in table.columns})
                                    rec['engine_admitted'] = engine._install(database, target=(a, b))
                                except (ValueError, TypeError, SyntaxError) as exc:
                                    rec['parse_error'] = str(exc)[:200]
                                validation_s += time.perf_counter() - t
                    if proc.poll() is not None:
                        if output.exists() and output.stat().st_size > tail.offset:
                            continue
                        driver_status = f'exit_{proc.returncode}'
                        break
                    if engine.remaining() <= 0:
                        driver_status = 'budget_terminated'
                        os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait()
                        break
                    time.sleep(0.05)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        engine.metrics['refutation_search_s'] += max(0.0, time.perf_counter() - native_start - validation_s)
        if driver_status != 'exit_0' and driver_status != 'budget_terminated':
            raise RuntimeError(f'official SQLWitness driver failed: {driver_status}; see {log}')
    partition_wall = time.perf_counter() - engine.started
    engine.assert_invariants(replay=True)
    result = engine.snapshot()
    result['unprocessed_query_ids'] = [q for q, _ in sqls[len(engine.arrival):]]
    result['metrics']['input_queries'] = len(sqls)
    result['metrics']['processed_queries'] = len(engine.arrival)
    result['metrics']['unprocessed_queries'] = len(result['unprocessed_query_ids'])
    result['shell'] = 'all'
    result['tool'] = 'sqlwitness'
    result['native_mode'] = 'official_experiment_driver_one_process_per_batch'
    result['official_driver_status'] = driver_status
    result['official_driver_requested_pairs'] = len(targets)
    result['calls'] = calls
    result['metrics']['native_pair_calls'] = len(calls)
    result['metrics']['pair_decision_requests'] = len(calls)
    result['metrics']['solver_calls'] = 0
    result['native_solver_coverage'] = 'SQLWitness native solver calls are opaque; released artifact CSV rows are reported separately'
    result['metrics']['partition_wall_s'] = partition_wall
    result['metrics']['throughput_queries_s'] = len(engine.arrival) / result['metrics']['wall_s']
    result['resource_scope'] = 'host process and waited child CPU; external DBMS CPU excluded; peak_rss_kib is parent only'
    return engine, result
