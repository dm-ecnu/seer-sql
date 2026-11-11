import itertools
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import pytest
from seer.pair_state import PairSeer
from seer2.schema import Schema
from seer2.refine import Out

DB={'t':[{'x':1},{'x':2}]}
QUERIES=[('a','SELECT x FROM t ORDER BY x ASC'),('d','SELECT x FROM t ORDER BY x DESC'),('b','SELECT x FROM t WHERE 0')]

def engine(shared,queries=QUERIES):
 return PairSeer(Schema('CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);','sqlite'),queries,
                 dialect='sqlite',backend='sqlite',fast=False,symbolic=False,budget=60,shared_pair_replay=shared)

class CountBackend:
 def __init__(self,wrapped):self.wrapped=wrapped;self.loads=0
 def __getattr__(self,name):return getattr(self.wrapped,name)
 def open(self,*a,**kw):self.loads+=1;return self.wrapped.open(*a,**kw)

def observe(seer):
 return ({p:seer.verdict(*p) for p in itertools.combinations(seer.queries,2)},seer.open_pairs,seer.dropped)


def test_fresh_snapshot_is_shared_without_merging_pair_claims():
 queries=[(f'u{i}','SELECT x FROM t') for i in range(3)]
 queries += [(f'a{i}','SELECT x FROM t ORDER BY x ASC') for i in range(3)]
 queries += [(f'd{i}','SELECT x FROM t ORDER BY x DESC') for i in range(3)]
 a,b=engine(False,queries),engine(True,queries)
 assert a.run_db(DB,'native')==b.run_db(DB,'native')==9
 a.backend=CountBackend(a.backend);b.backend=CountBackend(b.backend)
 ea,eb=a.stats['executions'],b.stats['executions']
 a.recheck();b.recheck()
 assert observe(a)==observe(b)
 assert a.backend.loads==9 and b.backend.loads==1
 assert a.stats['executions']-ea==18 and b.stats['executions']-eb==6
 assert a.stats['recheck_ok']==b.stats['recheck_ok']==9
 assert len(a.events)==len(b.events)==9


@pytest.mark.parametrize('order',list(itertools.permutations(range(3))))
def test_pair_specific_error_retries_withdraw_only_affected_claims(order):
 queries=[QUERIES[i] for i in order]
 engines=[engine(False,queries),engine(True,queries)]
 for s in engines:
  assert s.run_db(DB,'native')==3
  original=s._exec_all
  def faulty(inst,db,qs,original=original):
   outs=original(inst,db,qs)
   if 'b' in qs:outs['b']=Out('err',msg='injected replay timeout')
   return outs
  s._exec_all=faulty;s.backend=CountBackend(s.backend);s.recheck()
 assert observe(engines[0])==observe(engines[1])
 assert engines[1].verdict('a','d')['verdict']=='split'
 assert engines[1].verdict('a','b')['verdict']=='undecided'
 assert engines[1].stats['recheck_ok']==1 and engines[1].stats['recheck_dropped']==2
 assert engines[0].backend.loads==7 and engines[1].backend.loads==3


def test_retry_round_can_recover_edges_without_replaying_already_passed_edges():
 result=[]
 for shared in (False,True):
  s=engine(shared);assert s.run_db(DB,'native')==3
  original=s._exec_all;seen={'bad_query_calls':0}
  def temporary(inst,db,qs,original=original,seen=seen):
   outs=original(inst,db,qs)
   if 'b' in qs:
    seen['bad_query_calls']+=1
    if seen['bad_query_calls']<=2:outs['b']=Out('err',msg='temporary failure')
   return outs
  s._exec_all=temporary;s.recheck();result.append(s)
 assert observe(result[0])==observe(result[1])
 assert result[1].stats['recheck_ok']==3 and not result[1].dropped


def test_index_observes_late_withdrawal_and_new_pair_proofs():
 s=engine(True,QUERIES[:2]);assert s.run_db(DB,'native')==1
 assert s.pair_certificate('a','d')==0
 s.dbs.append(s.dbs[0].copy());s.events.append((1,['a','d'],[['a'],['d']]))
 s.dropped.add(0)
 assert s.pair_certificate('d','a')==1
 s.dropped.add(1)
 assert s.pair_certificate('a','d') is None
 assert s.verdict('a','d')['verdict']=='undecided'


def test_indexed_verdict_does_not_scan_the_entire_event_log():
 s=engine(True,QUERIES[:2]);s.run_db(DB,'native')
 assert s.pair_certificate('a','d')==0
 class NoIterList(list):
  def __iter__(self):raise AssertionError('linear event scan during indexed verdict')
 s.events=NoIterList(s.events)
 for _ in range(100):assert s.pair_certificate('a','d')==0


def test_withdrawn_event_is_never_revived_by_a_later_shared_replay():
 s=engine(True,QUERIES[:2]);s.run_db(DB,'native');s.dropped.add(0)
 s.recheck()
 assert s.pair_certificate('a','d') is None and s.unsplit_pairs()==1


def test_nonquery_or_unknown_function_keeps_fresh_per_pair_replay():
 from sqlglot import parse_one
 s=engine(True);s.run_db(DB,'native')
 # Only the stored AST changes; execute the same read-only SQL fixture.
 s.prep['b'].tree=parse_one('SELECT unknown_udf(x) FROM t')
 s.backend=CountBackend(s.backend);s.recheck()
 assert s.backend.loads==3 and not s.dropped
 assert all(s.verdict(a,b)['verdict']=='split' for a,b in itertools.combinations(s.queries,2))


def test_complete_symbolic_controller_matches_original_pair_contract():
 queries=[('bridge','SELECT x FROM t'),('a','SELECT x FROM t ORDER BY x ASC'),('d','SELECT x FROM t ORDER BY x DESC')]
 engines=[]
 for shared in (False,True):
  s=PairSeer(Schema('CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);','sqlite'),queries,
             dialect='sqlite',backend='sqlite',fast=False,budget=10,levels=(1,2),max_k=2,
             shared_pair_replay=shared)
  s.run();engines.append(s)
 assert observe(engines[0])==observe(engines[1])
 assert engines[1].verdict('a','d')['verdict']=='split'
 assert engines[1].verdict('bridge','a')=={'verdict':'bounded_equivalent','k':2}
 assert engines[1].verdict('bridge','d')=={'verdict':'bounded_equivalent','k':2}


def test_pure_logic_outside_the_compiler_fragment_keeps_shared_replay():
 queries=[('a','SELECT x FROM t WHERE x > 0 AND x < 3 ORDER BY x ASC'),
          ('d','SELECT x FROM t WHERE NOT (x < 1 OR x > 2) ORDER BY x DESC'),
          ('b','SELECT x FROM t WHERE x > 10 AND x < 20')]
 s=engine(True,queries)
 s.ir.clear()  # Admission must recognize syntax, not rely on compiler support.
 assert all(s._shareable_replay_query(q) for q in s.queries)
 assert s.run_db(DB,'native')==3
 s.backend=CountBackend(s.backend);s.recheck()
 assert s.backend.loads==1 and s.stats['recheck_ok']==3
 assert not s.dropped
