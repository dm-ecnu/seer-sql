import ast
from pathlib import Path
import subprocess
import time
import pytest
import seer.encode as encode
from seer.encode import Domain,DomainInputs,schema_strings
from seer.front import Front
from seer.representative_search import RepresentativeSearch
from seer2.schema import Schema

@pytest.fixture(scope='module')
def original_domain():
 root=Path(__file__).resolve().parents[1]
 source=subprocess.check_output(['git','-C',str(root),'show','057149c65a4fbd5ae742345d24d3b306b6b62cfa:src/seer/encode.py'],text=True)
 cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='Domain')
 ns=dict(vars(encode));exec(compile(ast.Module(body=[cls],type_ignores=[]),'<frozen-057149c-Domain>','exec'),ns)
 return ns['Domain']

QUERIES=["SELECT s FROM t", "SELECT s FROM t WHERE s='a-later-literal'",
 "SELECT LOWER(s) FROM t", "SELECT s FROM t WHERE s LIKE '%needle_%'",
 "SELECT REPLACE(s, 'a', 'zz') FROM t", "SELECT SUBSTRING(s, 1, 3) FROM t",
 "SELECT d FROM t WHERE d >= DATE '2020-01-01'",
 "SELECT d FROM t WHERE d < DATE '1985-01-01'", "SELECT s FROM t WHERE s='ZZ Later'"]

@pytest.mark.parametrize('max_pool',[8,12,1500])
@pytest.mark.parametrize('rounds',[0,1,2])
def test_every_stream_prefix_matches_original_whole_batch(original_domain,max_pool,rounds):
 s=Schema("CREATE TABLE t(s TEXT CHECK(s <> 'schema-lit'), d DATE);",'sqlite');f=Front(s,'sqlite')
 inputs=DomainInputs(extra_strings=['external'],checks_strings=schema_strings(s),max_pool=max_pool,closure_rounds=rounds)
 rels=[]
 for sql in QUERIES:
  r=f.translate(sql);rels.append(r);inputs.add([r]);actual=inputs.build()
  expected=original_domain.build(rels,extra_strings=['external'],checks_strings=schema_strings(s),max_pool=max_pool,closure_rounds=rounds)
  assert actual.strings==expected.strings and actual.index==expected.index
  assert (actual.dlo,actual.dhi,actual.month_starts)==(expected.dlo,expected.dhi,expected.month_starts)
  assert Domain.build(rels,extra_strings=['external'],checks_strings=schema_strings(s),max_pool=max_pool,closure_rounds=rounds).strings==expected.strings


def test_each_ir_scanned_only_on_arrival_and_unchanged_facts_reuse_domain(monkeypatch):
 s=Schema('CREATE TABLE t(x INT);','sqlite');f=Front(s,'sqlite')
 rels=[f.translate('SELECT x FROM t WHERE x='+str(i)) for i in range(64)]
 original=encode.ir.walk_exprs;seen=[]
 def observed(r):seen.append(id(r));yield from original(r)
 monkeypatch.setattr(encode.ir,'walk_exprs',observed)
 inputs=DomainInputs();domain=inputs.build()
 for r in rels:inputs.add([r]);assert inputs.build() is domain
 assert seen==list(map(id,rels))
 assert inputs.relation_roots_seen==64 and inputs.materializations==1


def test_old_string_map_applies_to_new_literals_and_new_map_to_old_literals(original_domain):
 s=Schema('CREATE TABLE t(s TEXT);','sqlite');f=Front(s,'sqlite');inputs=DomainInputs();rels=[]
 for sql in ["SELECT LOWER(s) FROM t","SELECT 'NEW Mixed' FROM t","SELECT REPLACE(s,'e','ABC') FROM t"]:
  r=f.translate(sql);rels.append(r);inputs.add([r])
  assert inputs.build().strings==original_domain.build(rels).strings
 assert 'new mixed' in inputs.build().strings


def test_warm_level_does_not_scan_previously_registered_ir_dict():
 s=RepresentativeSearch(Schema('CREATE TABLE t(x INT);','sqlite'),'sqlite')
 s.add_query('a','SELECT x FROM t');s.add_query('b','SELECT x FROM t')
 assert s.search('a','b',3,bounds=(1,))['status']=='domain_unsat'
 lv=s.levels[1];out=lv.out['a']
 class NoFullScan(dict):
  def items(self):raise AssertionError('full IR scan')
  def values(self):raise AssertionError('full IR scan')
  def __iter__(self):raise AssertionError('full IR scan')
 s.irs=NoFullScan(s.irs)
 s.add_query('c','SELECT x FROM t WHERE x=1')
 r=s.search('a','c',3,bounds=(1,))
 assert r['status']=='candidate' and s.levels[1] is lv and lv.out['a'] is out
 assert lv._stream_registered==3


def test_representative_search_stops_before_oversized_native_check():
 s=RepresentativeSearch(Schema('CREATE TABLE t(x INT);','sqlite'),'sqlite',max_side_constraints=0)
 s.add_query('a','SELECT x FROM t')
 s.add_query('b','SELECT x FROM t WHERE x=1')
 r=s.search('a','b',3,bounds=(1,))
 assert r['status']=='undecided' and r['reason']=='resource_limit'
 assert r['side_constraints']>0 and r['k']==1
 assert s.levels[1].checks==0
