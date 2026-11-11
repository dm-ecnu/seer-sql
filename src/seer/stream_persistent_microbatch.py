"""Arrival-time SQLWitness microbatches using one author-code worker per stream."""
from __future__ import annotations

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import csv
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import time

from .official_batch import materialize, mysql_connection_failure
from .stream_microbatch import _database, _pairs
from .class_ablation import decision_pool


class PersistentOfficialBatch:
    def __init__(self, engine, eval_schema, dialect, evidence_dir, seed):
        self.engine, self.eval_schema, self.dialect, self.seed = engine, eval_schema, dialect, seed
        self.root = Path(evidence_dir).parent / 'sqlwitness-microbatches'
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = (self.root / 'worker-stderr.log').open('ab')
        self.scratch = self.root / 'worker-sqlite-scratch'
        self.scratch.mkdir(exist_ok=True)
        self.process = None
        self.seen_pairs = set()
        engine.metrics.update(microbatch_rounds=0, microbatch_pairs=0,
                              microbatch_dispatched_pairs=0, microbatch_native_s=0.,
                              microbatch_materialize_s=0., microbatch_native_reported_s=0.,
                              microbatch_candidates=0, microbatch_feedback_interrupts=0,
                              microbatch_worker_starts=0, microbatch_error_rows=0, microbatch_success_rows=0)

    def _start(self):
        if self.process is not None:
            return
        command = [(SEER_ROOT + '/repro/sqlwitness/venv-sw-py311-pinned/bin/python'),
                   '-u', str(Path(__file__).resolve().parents[2] / 'scripts/sqlwitness_batch_worker.py')]
        self.process = subprocess.Popen(command, cwd=self.scratch, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.log,
                                        start_new_session=True, text=True, bufsize=1,
                                        env=os.environ.copy())
        self.engine.metrics['microbatch_worker_starts'] += 1

    def _send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def _read(self):
        seconds = max(0., self.engine.remaining() - self.engine.finalization_reserve_s)
        if not seconds or not select.select([self.process.stdout], [], [], seconds)[0]:
            raise TimeoutError('official SQLWitness stream budget exhausted')
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError('official SQLWitness worker exited; see ' + str(self.root / 'worker-stderr.log'))
        return json.loads(line)

    def run_pending(self, min_pairs=1):
        engine = self.engine
        while engine.remaining() > engine.finalization_reserve_s:
            with decision_pool(engine, 'quick_selection'):
                pairs = _pairs(engine, self.seen_pairs)
            if len(pairs) < min_pairs:
                break
            round_no = engine.metrics['microbatch_rounds']
            work = self.root / f'round-{round_no:05d}'
            stamp = time.perf_counter()
            bench, mapping = materialize(work, self.eval_schema,
                                          'postgresql' if self.dialect == 'postgres' else self.dialect,
                                          pairs)
            engine.metrics['microbatch_materialize_s'] += time.perf_counter() - stamp
            engine.metrics['microbatch_rounds'] += 1
            engine.metrics['microbatch_dispatched_pairs'] += len(pairs)
            self._start()
            self._send({'action': 'run', 'root': str(work), 'benchmark': bench, 'seed': self.seed, 'per_call_s':engine.per_call_s})
            output = work / 'official-batch.csv'
            rows = 0
            admitted = 0
            candidates = 0
            native_s = 0.
            with output.open('w', newline='', encoding='utf-8') as handle:
                writer = csv.writer(handle)
                while True:
                    wait_start = time.perf_counter()
                    try:
                        response = self._read()
                    except TimeoutError:
                        engine.events.append({'event': 'microbatch_round_complete',
                                              'round': round_no, 'rows': rows,
                                              'status': 'budget_terminated'})
                        self.close()
                        return
                    native_s += time.perf_counter() - wait_start
                    if response['kind'] == 'done':
                        break
                    if response['kind'] != 'row':
                        raise RuntimeError('invalid official SQLWitness worker response')
                    if rows == 0:
                        writer.writerow(response['columns'])
                    writer.writerow(response['row'])
                    handle.flush()
                    row = dict(zip(response['columns'], map(str, response['row'])))
                    if mysql_connection_failure(row):
                        raise RuntimeError('official SQLWitness MySQL connection failure; see ' + str(output))
                    key = ((row['ProblemID'], row['QueryID']) if bench == 'leetcode'
                           else (row['Index'], row['Schema']))
                    error = row.get('Error', '')
                    if error:
                        engine.metrics['microbatch_error_rows'] += 1
                        assert not ('No terminal matches' in error and any(x in error for x in ('row_dep:', 'col_dep:', 'col_val:'))), \
                            'SQLWitness YAML constraint transport failed'
                    else:
                        engine.metrics['microbatch_success_rows'] += 1
                    a, b = mapping[key]
                    self.seen_pairs.add((a, b))
                    engine._quick_pair_origin[(a,b)] = 'arrival'
                    engine._arrival_requested.add(b)
                    rows += 1
                    engine.metrics['microbatch_pairs'] += 1
                    engine.metrics['quick_witness_calls'] += 1
                    engine.metrics['pair_decision_requests'] += 1
                    if row.get('Time'):
                        engine.metrics['microbatch_native_reported_s'] += float(row['Time'])
                    engine.events.append({'event': 'microbatch_native', 'round': round_no,
                                          'row': rows, 'target': (a, b),
                                          'wall_s': time.perf_counter() - engine.started})
                    value = row.get('Counterexample', '')
                    if row.get('Refuted') == 'True' and value and value != 'COUNTEREXAMPLE_FOUND':
                        from .official_batch import parse_counterexample
                        try:
                            database = _database(value, self.eval_schema)
                        except (ValueError, TypeError, SyntaxError):
                            engine.metrics['rejected_candidates'] += 1
                        else:
                            candidates += 1
                            engine.metrics['microbatch_candidates'] += 1
                            with decision_pool(engine, 'quick_install'):
                                admitted += engine.install_batch([(database, (a, b))])
                    self._send({'action': 'stop' if admitted else 'continue'})
            engine.metrics['microbatch_native_s'] += native_s
            engine.metrics['refutation_search_s'] += native_s
            if admitted:
                engine.metrics['microbatch_feedback_interrupts'] += 1
            engine.events.append({'event': 'microbatch_round_complete', 'round': round_no,
                                  'dispatched_pairs': len(pairs), 'rows': rows,
                                  'candidates': candidates, 'admitted': admitted,
                                  'native_wall_s': native_s, 'status': 'feedback' if admitted else 'complete'})
            if not admitted and rows != len(pairs):
                raise RuntimeError(f'official SQLWitness returned {rows}/{len(pairs)} rows')

    def close(self):
        proc = self.process
        self.process = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.stdin.write('{"action":"close"}\n')
                    proc.stdin.flush()
                    proc.wait(timeout=2)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            finally:
                proc.stdin.close()
                proc.stdout.close()
        if not self.log.closed:
            self.log.close()
