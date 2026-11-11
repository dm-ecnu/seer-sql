import time
import pytest
import z3
from seer.representative_search import RepresentativeSearch
from seer2.schema import Schema
from seer2.backends import make_backend
from seer2.refine import Prepared,execute
from seer.core import split_class_n


def session(sql, ddl='CREATE TABLE t(x INTEGER);'):
 s=RepresentativeSearch(Schema(ddl,'sqlite'),'sqlite')
 for qid,q in enumerate(sql):s.add_query(str(qid),q)
 return s


def fresh_replay(s,r,a='0',b='1'):
 assert r['status']=='candidate' and not r['proof_admitted']
 for _ in range(2):
  inst=make_backend('sqlite',s.schema,2).open(r['database'])
  try:outs={q:execute(inst,Prepared(s.queries[q],'sqlite','sqlite')) for q in (a,b)}
  finally:inst.close()
  assert len(split_class_n([a,b],outs))==2


def test_null_bag_target_has_fresh_sqlite_certificates():
 s=session(['SELECT COUNT(*) FROM t','SELECT COUNT(x) FROM t'])
 r=s.search('0','1',3,bounds=(1,2));fresh_replay(s,r)
 assert r['levels'][0]['solver_calls']==1


def test_is_not_null_keeps_negation_in_bounded_search():
 s=session(['SELECT x FROM t WHERE x IS NULL',
            'SELECT x FROM t WHERE x IS NOT NULL'])
 assert repr(s.irs['0']) != repr(s.irs['1'])
 r=s.search('0','1',3,bounds=(1,))
 fresh_replay(s,r)


def test_not_like_keeps_negation_in_search_and_engine_replay():
 s=session(["SELECT x FROM t WHERE x LIKE 'Cs'",
            "SELECT x FROM t WHERE x NOT LIKE 'Cs'"],
           ddl='CREATE TABLE t(x TEXT);')
 assert repr(s.irs['0']) != repr(s.irs['1'])
 r=s.search('0','1',3,bounds=(1,))
 fresh_replay(s,r)


def test_not_ilike_keeps_negation_in_postgres_ir():
 s=RepresentativeSearch(Schema('CREATE TABLE t(x TEXT);','postgres'),'postgres')
 s.add_query('like',"SELECT x FROM t WHERE x ILIKE 'Cs'")
 s.add_query('not_like',"SELECT x FROM t WHERE x NOT ILIKE 'Cs'")
 assert repr(s.irs['like']) != repr(s.irs['not_like'])


def test_same_domain_new_query_reuses_shared_level_and_existing_output():
 s=session(['SELECT x FROM t','SELECT x FROM t WHERE x IS NOT NULL'])
 r=s.search('0','1',3,bounds=(1,));fresh_replay(s,r)
 lv=s.levels[1];output=lv.out['0'];solver=lv.solver
 s.add_query('2','SELECT x FROM t WHERE x=0')
 r=s.search('0','2',3,bounds=(1,))
 fresh_replay(s,r,'0','2')
 assert s.levels[1] is lv and lv.out['0'] is output and lv.solver is solver
 assert r['levels'][0]['encoding_reused']


def test_identical_relations_share_encoded_output():
 s=session(['SELECT x FROM t','SELECT x FROM t'])
 r=s.search('0','1',3,bounds=(1,2))
 assert r['status']=='domain_unsat' and r['k']==2 and not r['proof_admitted']
 assert s.levels[1].out['0'] is s.levels[1].out['1']
 assert r['scope']=='encoded_fragment_fixed_value_domain_both_executions_valid'
 assert len(r['levels'])==2


def test_new_string_literal_invalidates_old_level_and_unsat_identity():
 s=session(['SELECT x FROM t','SELECT x FROM t'],ddl='CREATE TABLE t(x TEXT);')
 old=s.search('0','1',3,bounds=(1,));old_level=s.levels[1]
 s.add_query('2',"SELECT x FROM t WHERE x='new-stream-literal'")
 assert not s.levels and s.domain_revision>old['domain_revision']
 r=s.search('0','2',3,bounds=(1,));fresh_replay(s,r,'0','2')
 assert r['domain_sha256']!=old['domain_sha256'] and s.levels[1] is not old_level


def test_auxiliary_objective_never_replaces_mandatory_target():
 s=session(['SELECT x FROM t','SELECT x FROM t WHERE x=0',
            'SELECT x FROM t WHERE x=1','SELECT x FROM t WHERE x=2'])
 r=s.search('0','1',3,bounds=(2,),representative_pairs=[('2','3'),('0','3')])
 fresh_replay(s,r)


def test_budget_does_not_produce_unsat():
 s=session(['SELECT x FROM t','SELECT x FROM t'])
 r=s.search('0','1',0)
 assert r['status']=='undecided' and not s.levels


def test_unknown_does_not_become_unsat(monkeypatch):
 s=session(['SELECT x FROM t','SELECT x FROM t'])
 lv,_=s._level(1,time.perf_counter()+3)
 monkeypatch.setattr(lv,'check',lambda _: (z3.unknown,None))
 assert s.search('0','1',1,bounds=(1,))['status']=='undecided'


def test_unsupported_input_is_explicit():
 s=session(['SELECT x FROM t','SELECT randomblob(x) FROM t'])
 r=s.search('0','1',3,bounds=(1,))
 assert r['status']=='unsupported' and not r['proof_admitted']


def test_partially_failed_extension_never_poisons_existing_search(monkeypatch):
 s=session(['SELECT x FROM t','SELECT x FROM t'])
 assert s.search('0','1',3,bounds=(1,))['status']=='domain_unsat'
 lv=s.levels[1]
 s.add_query('2','SELECT x FROM t WHERE x=0')
 def fail(*args):
  lv.solver.add(z3.BoolVal(False))
  raise TimeoutError('partial encoding')
 monkeypatch.setattr(lv,'extend',fail)
 r=s.search('0','2',3,bounds=(1,));fresh_replay(s,r,'0','2')
 assert s.levels[1] is not lv


def test_declared_nonddl_inclusion_is_bound_in_schema_identity():
 ddl='CREATE TABLE t(x INTEGER); CREATE TABLE u(x INTEGER);'
 a=RepresentativeSearch(Schema(ddl,'sqlite'),'sqlite')
 b=RepresentativeSearch(Schema(ddl,'sqlite',inclusions=[('t',['x'],'u',['x'])]),'sqlite')
 for s in (a,b):
  s.add_query('0','SELECT x FROM t');s.add_query('1','SELECT x FROM t')
 ra=a.search('0','1',3,bounds=(1,));rb=b.search('0','1',3,bounds=(1,))
 assert ra['schema_sha256'] != rb['schema_sha256']


def test_default_schedule_deepens_until_budget_not_fixed_k4(monkeypatch):
 s=session(['SELECT x FROM t','SELECT x FROM t'])
 called=[]
 class FakeLevel:
  checks=0;solve_s=0.;unsup={}
  enc=type("EncSide",(),{"side":[]})()
  def ok(self,q):return True
  def valid(self,qs):return z3.BoolVal(True)
  def sep(self,a,b):return z3.BoolVal(False)
  def check(self,goal):
   self.checks+=1
   return (z3.unknown if called[-1]>=8 else z3.unsat),None
 def fake(k,deadline):called.append(k);return FakeLevel(),False
 monkeypatch.setattr(s,'_level',fake)
 assert s.search('0','1',1)['status']=='undecided'
 assert called==[1,2,4,8]


def test_hard_timeout_terminates_blocked_symbolic_worker(monkeypatch):
 s=RepresentativeSearch(Schema('CREATE TABLE t(x INT);','sqlite'),'sqlite',hard_timeout=True)
 s.add_query('a','SELECT x FROM t')
 s.add_query('b','SELECT x FROM t WHERE x=1')
 def hang(*args):time.sleep(10)
 monkeypatch.setattr(s,'_search_inner',hang)
 start=time.perf_counter();r=s.search('a','b',0.05)
 assert r['status']=='undecided' and r['reason']=='hard_timeout'
 assert time.perf_counter()-start<1

def test_hard_timeout_worker_returns_real_candidate():
 s=RepresentativeSearch(Schema('CREATE TABLE t(x INT);','sqlite'),'sqlite',hard_timeout=True)
 s.add_query('a','SELECT x FROM t')
 s.add_query('b','SELECT x FROM t WHERE x=1')
 r=s.search('a','b',3,bounds=(1,))
 assert r['status']=='candidate' and r['database']


def test_persistent_worker_reuses_encoded_level():
 s=RepresentativeSearch(Schema('CREATE TABLE t(x INT);','sqlite'),'sqlite',persistent_worker=True)
 try:
  s.add_query('a','SELECT x FROM t')
  s.add_query('b','SELECT x FROM t WHERE x=1')
  first=s.search('a','b',3,bounds=(1,))
  pid=s._level_workers[1][0].pid
  second=s.search('a','b',3,bounds=(1,))
  assert first['status']==second['status']=='candidate'
  assert second['levels'][0]['encoding_reused'] and s._level_workers[1][0].pid==pid
 finally:s.close_worker()

def test_persistent_worker_has_process_deadline(monkeypatch):
 import seer.representative_search as module
 def hang(*args):time.sleep(10)
 monkeypatch.setattr(module,'_persistent_search_worker',hang)
 s=RepresentativeSearch(Schema('CREATE TABLE t(x INT);','sqlite'),'sqlite',persistent_worker=True)
 s.add_query('a','SELECT x FROM t');s.add_query('b','SELECT x FROM t WHERE x=1')
 start=time.perf_counter();r=s.search('a','b',0.05)
 assert r['status']=='undecided' and r['reason']=='hard_timeout'
 assert time.perf_counter()-start<1 and s._worker is None


def test_persistent_worker_retains_verified_bounded_prefix_at_timeout(monkeypatch):
 def proof_then_hang(self, query, representative, budget_s, bounds=None,
                     representative_pairs=(), augment=3, canonical_contract=False, progress=None):
  assert progress is not None
  progress({'query':query,'representative':representative,'domain_sha256':self.domain_identity(),
            'bounded_k':1,'proof_admitted':False,'levels':[{'k':1,'canonical_admissible':True,
            'solver_calls':2,'answer':'unsat'}]})
  time.sleep(10)
 monkeypatch.setattr(RepresentativeSearch,'_search_inner',proof_then_hang)
 s=RepresentativeSearch(Schema('CREATE TABLE t(x INT);','sqlite'),'sqlite',persistent_worker=True)
 s.add_query('a','SELECT x FROM t');s.add_query('b','SELECT x FROM t WHERE x=1')
 r=s.search('a','b',0.25,canonical_contract=True)
 assert r['status']=='undecided' and r['reason']=='hard_timeout_after_bounded_progress'
 assert r['bounded_k']==1 and r['levels'][0]['canonical_admissible']
 assert s._worker is None
