"""Persistent control loop around the released SQLWitness experiment functions.

The author artifact is imported unchanged. Each command swaps only the generated
benchmark input root; a row is acknowledged before the next pair starts so a
validated witness can split the stream before more native requests are made.
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import json
from contextlib import redirect_stdout
from pathlib import Path
import random
import sys
import time

ARTIFACT = Path((SEER_ROOT + '/datasets/raw/sqlwitness/x/SQLWitness-artifact'))
sys.path.insert(0, str(ARTIFACT))
sys.path.insert(0, str(ARTIFACT / 'experiments'))
import benchmarks
import run_sqlwitness as official
from sqlwitness.online.runner import DatabaseRunner

# Same SQLite storage adapter as the frozen run_official_batch path. Only the
# private database location changes; author SQLWitness code and data are intact.
_database_init = DatabaseRunner.__init__
def _memory_init(self, *args, **kwargs):
    _database_init(self, *args, **kwargs)
    if self.dialect_name == 'sqlite':
        self.db_file = ':memory:'
DatabaseRunner.__init__ = _memory_init


def send(value):
    print(json.dumps(value, default=str), flush=True)


def main():
    while command_line := sys.stdin.readline():
        command = json.loads(command_line)
        if command['action'] == 'close':
            return
        if command['action'] != 'run':
            raise ValueError('unknown worker action')
        root = Path(command['root'])
        benchmark = command['benchmark']
        benchmarks.BENCH_DIR = str(root / 'benchmarks')
        dialect = benchmarks.DIALECTS[benchmark]
        if dialect != 'sqlite':
            DatabaseRunner.connection_url(dialect)
        random.seed(int(command['seed']))
        build_row = official.leetcode_row if benchmark == 'leetcode' else official.paired_row
        columns = official.LEETCODE_COLUMNS if benchmark == 'leetcode' else official.PAIRED_COLUMNS
        for case in benchmarks.load_cases(benchmark):
            found, data, elapsed, iterations, error = False, None, 0.0, None, None
            started = time.time()
            if case.schema is not None:
                try:
                    with redirect_stdout(sys.stderr):
                        found, data, elapsed, iterations = official.counterexample(
                            case.schema, case.constraints, case.groundtruth, case.candidate,
                            timeout=float(command.get("per_call_s",60)), dialect=dialect, termination_target_risk=0.05,
                            remove_null=False, remove_one=False, remove_literal=False)
                except Exception as exc:
                    error, elapsed = str(exc), time.time() - started
            row = build_row(case, found, data, elapsed, iterations, error)
            send({'kind': 'row', 'columns': columns, 'row': row})
            ack = json.loads(sys.stdin.readline())
            if ack['action'] == 'stop':
                break
            if ack['action'] != 'continue':
                raise ValueError('invalid row acknowledgement')
        send({'kind': 'done'})


if __name__ == '__main__':
    main()
