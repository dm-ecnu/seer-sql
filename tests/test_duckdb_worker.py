"""Real DuckDB/Arrow work must fit the unchanged TasksMax=128 service."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'src')]
from hard_deadline import initialize_duckdb_worker

def test_original_arrow_truth_table_runs_with_bounded_threads():
    runtime=initialize_duckdb_worker()
    assert runtime['threads_after_default_close']<16
    from seer2.generate import AtomEval
    from seer2.schema import INT
    atom=AtomEval(engine='mysql',dialect='mysql')
    try:
        assert atom.unary('x0 = 1',[None,1,2],INT)==['N','T','F']
        assert atom.unary('x0 IS NULL',[None,1,2],INT)==['T','F','F']
        assert len(list(Path('/proc/self/task').iterdir()))<16
    finally:atom.con.close()

def test_unused_default_cleanup_preserves_existing_explicit_connections():
    initialize_duckdb_worker()
    import duckdb
    first=duckdb.connect(config={'threads':1})
    second=duckdb.connect(config={'threads':1})
    try:
        first.execute('CREATE TABLE t AS SELECT 42 AS x')
        initialize_duckdb_worker()
        assert first.execute('SELECT x FROM t').fetchall()==[(42,)]
        assert second.execute('SELECT 7').fetchall()==[(7,)]
        assert len(list(Path('/proc/self/task').iterdir()))<16
    finally:
        first.close();second.close()
