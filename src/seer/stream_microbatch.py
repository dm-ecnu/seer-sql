"""Representative SQLWitness microbatches with immediate witness feedback."""
from __future__ import annotations

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import csv
import os
from pathlib import Path
import signal
import subprocess
import time

from seereval.engine import _coerce
from .official_batch import _CsvTail, materialize, parse_counterexample, mysql_connection_failure


def _database(candidate, eval_schema):
    original = parse_counterexample(candidate)
    database = {}
    for table in eval_schema.tables:
        rows = next((r for name, r in original.items() if name.lower() == table.name.lower()), [])
        database[table.name.lower()] = []
        for values in rows:
            low = {str(k).lower(): v for k, v in values.items()}
            database[table.name.lower()].append({
                c['name'].lower(): _coerce(low.get(c['name'].lower()), c['type'])
                for c in table.columns})
    return database


def _pairs(engine, seen_pairs=frozenset()):
    """Choose unresolved representative/member pairs in the largest class."""
    if getattr(engine, '_class_arrival_only', False) and (
            engine.no_fingerprint_grouping or engine.no_certificate_transfer):
        return [(a, b, engine.queries[a], engine.queries[b])
                for a, b in engine._class_arrival_pairs if (a, b) not in seen_pairs
                and (engine.no_fingerprint_grouping or engine.fingerprints[a] == engine.fingerprints[b])]
    order = {q: i for i, q in enumerate(engine.arrival)}
    classes = []
    for fp, members in engine.index.items():
        rep = engine.representatives[fp]
        pending = [q for q in members if q != rep and (rep, q) not in seen_pairs
                   and engine._path(q, rep) is None]
        if pending:
            classes.append((len(members), order[rep], rep, pending))
    if not classes:
        return []
    _, _, rep, pending = sorted(classes, key=lambda c: (-c[0], c[1]))[0]
    return [(rep, q, engine.queries[rep], engine.queries[q])
            for q in sorted(pending, key=order.__getitem__)]


def _stop(proc):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def run_microbatch(engine, eval_schema, dialect, evidence_dir, method_seed):
    """Replay every new witness, split all arrived queries, then reschedule."""
    root = Path(evidence_dir).parent / 'sqlwitness-microbatches'
    root.mkdir(parents=True, exist_ok=True)
    engine.metrics.update(microbatch_rounds=0, microbatch_pairs=0,
                          microbatch_dispatched_pairs=0, microbatch_native_s=0.,
                          microbatch_materialize_s=0., microbatch_native_reported_s=0.,
                          microbatch_candidates=0, microbatch_feedback_interrupts=0,
                          microbatch_rows_accounted_after_interrupt=0)
    seen_pairs = set()
    while engine.remaining() > engine.finalization_reserve_s:
        pairs = _pairs(engine, seen_pairs)
        if not pairs:
            break
        round_no = engine.metrics['microbatch_rounds']
        work = root / f'round-{round_no:03d}'
        started = time.perf_counter()
        bench, mapping = materialize(work, eval_schema,
                                     'postgresql' if dialect == 'postgres' else dialect,
                                     pairs)
        engine.metrics['microbatch_materialize_s'] += time.perf_counter() - started
        engine.metrics['microbatch_rounds'] += 1
        engine.metrics['microbatch_dispatched_pairs'] += len(pairs)
        (work / 'sitecustomize.py').write_text(f'import random\nrandom.seed({int(method_seed)})\n')
        scratch = work / 'official-sqlite-scratch'
        scratch.mkdir()
        output = work / 'official-batch.csv'
        log = work / 'official-batch.log'
        command = ['unshare', '-Urm', '--', 'sh', '-c',
                   'mount -t tmpfs -o size=2g tmpfs "$1" && cd "$1" && exec "$2" "$3" --benchmark "$4" --output "$5"',
                   'sh', str(scratch),
                   (SEER_ROOT + '/repro/sqlwitness/venv-sw-py311-pinned/bin/python'),
                   str(work / 'experiments/run_sqlwitness.py'), bench, str(output)]
        env = os.environ.copy()
        env['PYTHONPATH'] = str(work) + os.pathsep + env.get('PYTHONPATH', '')
        env['PYTHONHASHSEED'] = str(int(method_seed))
        tail = _CsvTail(output)
        header = None
        rows = 0
        candidates = 0
        admitted = 0
        status = 'running'
        native_started = time.perf_counter()
        validation_s = 0.0
        with log.open('wb') as handle:
            proc = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT,
                                    start_new_session=True, env=env)
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
                        rows += 1
                        seen_pairs.add((a, b))
                        engine.metrics['microbatch_pairs'] += 1
                        engine.metrics['pair_decision_requests'] += 1
                        if row.get('Time'):
                            engine.metrics['microbatch_native_reported_s'] += float(row['Time'])
                        engine.events.append({'event': 'microbatch_native', 'round': round_no,
                                              'pairs': 1, 'row': rows,
                                              'wall_s': time.perf_counter() - engine.started,
                                              'target': (a, b)})
                        value = row.get('Counterexample', '')
                        if row.get('Refuted') != 'True' or not value or value == 'COUNTEREXAMPLE_FOUND':
                            continue
                        stamp = time.perf_counter()
                        try:
                            database = _database(value, eval_schema)
                        except (ValueError, TypeError, SyntaxError):
                            engine.metrics['rejected_candidates'] += 1
                            continue
                        candidates += 1
                        engine.metrics['microbatch_candidates'] += 1
                        admitted += engine.install_batch([(database, (a, b))])
                        validation_s += time.perf_counter() - stamp
                        if admitted:
                            status = 'feedback_interrupt'
                            break
                    if status == 'feedback_interrupt':
                        _stop(proc)
                        engine.metrics['microbatch_feedback_interrupts'] += 1
                        break
                    if proc.poll() is not None:
                        if output.exists() and output.stat().st_size > tail.offset:
                            continue
                        status = f'exit_{proc.returncode}'
                        break
                    if engine.remaining() <= engine.finalization_reserve_s:
                        status = 'budget_terminated'
                        _stop(proc)
                        break
                    time.sleep(0.05)
            finally:
                _stop(proc)
        elapsed = time.perf_counter() - native_started
        # The driver can flush another completed record while the first
        # witness is being replayed. Count these native calls even though the
        # new fingerprint split makes their candidates obsolete.
        if output.exists():
            with output.open(newline='', encoding='utf-8') as source:
                completed = list(csv.DictReader(source))
            for row in completed[rows:]:
                key = ((row['ProblemID'], row['QueryID']) if bench == 'leetcode'
                       else (row['Index'], row['Schema']))
                a, b = mapping[key]
                seen_pairs.add((a, b))
                engine.metrics['microbatch_pairs'] += 1
                engine.metrics['pair_decision_requests'] += 1
                engine.metrics['microbatch_rows_accounted_after_interrupt'] += 1
                rows += 1
                engine.events.append({'event': 'microbatch_native', 'round': round_no,
                                      'pairs': 1, 'row': rows, 'stale_after_feedback': True,
                                      'wall_s': time.perf_counter() - engine.started,
                                      'target': (a, b)})
        engine.metrics['microbatch_native_s'] += elapsed
        engine.metrics['refutation_search_s'] += max(0., elapsed - validation_s)
        engine.events.append({'event': 'microbatch_round_complete', 'round': round_no,
                              'dispatched_pairs': len(pairs), 'rows': rows,
                              'candidates': candidates, 'admitted': admitted,
                              'native_wall_s': elapsed, 'status': status})
        if status not in ('exit_0', 'feedback_interrupt', 'budget_terminated'):
            raise RuntimeError(f'official SQLWitness microbatch failed: {status}; see {log}')
        if status == 'budget_terminated':
            break
        if status == 'exit_0' and rows != len(pairs):
            raise RuntimeError(f'official SQLWitness returned {rows}/{len(pairs)} rows; see {output}')
