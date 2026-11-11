import sqlite3
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'eval'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'eval/tools'))
from seereval.schema import from_ddl
from seereval.engine import LoadError
from sqldriller_resource_view import write_database


def test_sample_prefix_reports_key_conflicts(tmp_path):
 s=from_ddl('s','CREATE TABLE t(id INTEGER PRIMARY KEY,v TEXT UNIQUE)',dialect='sqlite')
 target=tmp_path/'sample.sqlite'
 r=write_database(target,s,{'t':[{'id':1,'v':'a'},{'id':1,'v':'b'}]},sampling=True)
 assert r=={'source_rows':2,'sample_rows':1,'sample_rows_dropped':1}
 with sqlite3.connect(target) as c:
  assert c.execute('SELECT * FROM t').fetchall()==[(1,'a')]
  assert c.execute('PRAGMA quick_check').fetchone()==('ok',)


def test_invalid_witness_is_not_published(tmp_path):
 s=from_ddl('s','CREATE TABLE t(id INTEGER PRIMARY KEY)',dialect='sqlite')
 target=tmp_path/'witness.sqlite'
 with pytest.raises(LoadError):write_database(target,s,{'t':[{'id':1},{'id':1}]})
 assert not target.exists()


def test_valid_witness_keeps_declared_primary_key(tmp_path):
 s=from_ddl('s','CREATE TABLE t(a INTEGER,b INTEGER,PRIMARY KEY(a,b))',dialect='sqlite')
 target=tmp_path/'witness.sqlite'
 write_database(target,s,{'t':[{'a':1,'b':2},{'a':1,'b':3}]})
 with sqlite3.connect(target) as c:
  assert [(r[1],r[5]) for r in c.execute('PRAGMA table_info(t)')]==[('a',1),('b',2)]
  assert c.execute('SELECT * FROM t ORDER BY b').fetchall()==[(1,2),(1,3)]
  assert c.execute('PRAGMA quick_check').fetchone()==('ok',)
