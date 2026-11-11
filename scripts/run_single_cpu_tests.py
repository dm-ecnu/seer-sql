"""Initialize DuckDB/Arrow pools before running unchanged CPU-pinned tests.

DuckDB 1.5.6 eagerly creates a host-sized default pool on import. The owning
service must allow that transient initialization (e.g. TasksMax=512 on server),
while taskset/CPUAffinity still restrict all threads to one physical CPU.
"""
from pathlib import Path
import os
import sys


def main():
    affinity=sorted(os.sched_getaffinity(0))
    if len(affinity)!=1:
        raise RuntimeError('test process must be bound to one CPU')
    import duckdb
    imported=len(list(Path('/proc/self/task').iterdir()))
    duckdb.execute('SET threads=1')
    import pyarrow
    pyarrow.set_cpu_count(1)
    pyarrow.set_io_thread_count(1)
    print({'affinity':affinity,'duckdb_import_threads':imported,
           'initialized_threads':len(list(Path('/proc/self/task').iterdir())),
           'duckdb_threads':duckdb.execute("SELECT current_setting('threads')").fetchone()[0]},flush=True)
    import pytest
    return pytest.main(sys.argv[1:])


if __name__=='__main__':
    raise SystemExit(main())
