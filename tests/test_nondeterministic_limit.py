"""LIMIT tie completions and full native acceptance/proof scope."""
import itertools
import sys
from pathlib import Path
import pytest
import z3
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from seer.core import Seer,replay
from seer.pair_state import PairSeer
from seer.symbolic import Level
from seer.encode import Domain,schema_strings
from seer2.schema import Schema
DDL='CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL, y INTEGER NOT NULL);'

def engine(cls=PairSeer,queries=None):
 return cls(Schema(DDL,'sqlite'),queries or [('a','SELECT x FROM t'),('b','SELECT x FROM t LIMIT 2')],dialect='sqlite',backend='sqlite',fast=False,levels=(3,),max_k=3,budget=15,solver_timeout_ms=3000)

def fixed_level(sql,keys=(0,0,0),live=(True,True,True)):
 s=engine(queries=[('q',sql),('all','SELECT x FROM t')])
 lv=Level(s.S,s.front,Domain.build(list(s.ir.values()),checks_strings=schema_strings(s.S)),s.ir,3,'sqlite',timeout_ms=3000)
 for i,(exists,cells) in enumerate(lv.enc.vars['t']):
  lv.solver.add(exists==live[i],cells[0][0]==i+1,cells[1][0]==keys[i])
 return lv

def selection(lv,values):
 out=lv.out['q'];conditions=[]
 for active,cells in out.rows:
  conditions.append(active==z3.Or([cells[0][0]==v for v in values]))
 return z3.And(*conditions,lv.valid(['q']))

@pytest.mark.parametrize('suffix,size',[('LIMIT 1',1),('LIMIT 2',2),('LIMIT 1 OFFSET 1',1),('LIMIT 0',0)])
@pytest.mark.parametrize('order',['','ORDER BY y '])
def test_every_legal_tie_subset_is_represented(suffix,size,order):
 lv=fixed_level('SELECT x FROM t '+order+suffix)
 for values in itertools.chain.from_iterable(itertools.combinations((1,2,3),n) for n in range(4)):
  got,_=lv.check(selection(lv,values))
  assert (got==z3.sat)==(len(values)==size),(suffix,order,values,got)

@pytest.mark.parametrize('keys,expected',[((1,2,3),(1,2)),((3,1,2),(2,3)),((3,2,1),(2,3))])
def test_unique_order_keys_still_determine_membership(keys,expected):
 lv=fixed_level('SELECT x FROM t ORDER BY y LIMIT 2',keys)
 for values in itertools.combinations((1,2,3),2):
  got,_=lv.check(selection(lv,values))
  assert (got==z3.sat)==(values==expected)

def test_inactive_slots_do_not_change_limit_cardinality():
 lv=fixed_level('SELECT x FROM t LIMIT 2',live=(True,False,True))
 # Selection comparison explicitly ignores inactive input rows.
 active=[e for e,_ in lv.out['q'].rows]
 assert lv.check(z3.And(lv.valid(['q']),z3.Sum([z3.If(e,1,0) for e in active])!=2))[0]==z3.unsat

@pytest.mark.parametrize('cls',[Seer,PairSeer])
def test_complete_system_finds_native_cardinality_witness(cls):
 s=engine(cls);r=s.run()
 assert s.verdict('a','b')['verdict']=='split',r
 assert replay(s.S,r,dialect='sqlite',backend='sqlite')==1
 assert s.stats.get('model_mismatch',0)==0

@pytest.mark.parametrize('cls',[Seer,PairSeer])
def test_identical_limited_queries_share_same_encoding(cls):
 s=engine(cls,[('a','SELECT x FROM t LIMIT 2'),('b','SELECT x FROM t LIMIT 2')]);r=s.run()
 assert s.verdict('a','b')=={'verdict':'bounded_equivalent','k':3},r

@pytest.mark.parametrize('cls',[Seer,PairSeer])
def test_restricted_unsat_cannot_prove_excluded_native_models(monkeypatch,cls):
 s=engine(cls);original=Level.find;calls=[]
 def find(self,*args,**kwargs):
  calls.append(1)
  return original(self,*args,**kwargs) if len(calls)==1 else ('unsat',None,None)
 monkeypatch.setattr(Level,'find',find)
 monkeypatch.setattr(s,'run_db',lambda *args,**kwargs:0)
 r=s.run()
 assert len(calls)==2,r
 assert s.verdict('a','b')['verdict']=='undecided',r
 assert 'excluded_unconfirmed_models' in s.verdict('a','b')['reason']
 assert s.stats.get('bounded_proof_withheld',0)==1

@pytest.mark.parametrize('suffix,expected',[
 ('LIMIT 1',{(1,),(2,)}),
 ('LIMIT 2',{(1,2)}),
 ('LIMIT 2 OFFSET 1',{(1,3),(2,3)}),
])
def test_tie_permutations_cannot_cross_distinct_order_keys(suffix,expected):
 lv=fixed_level('SELECT x FROM t ORDER BY y '+suffix,keys=(0,0,1))
 for values in itertools.chain.from_iterable(itertools.combinations((1,2,3),n) for n in range(4)):
  got,_=lv.check(selection(lv,values))
  assert (got==z3.sat)==(values in expected),(suffix,values,got)
