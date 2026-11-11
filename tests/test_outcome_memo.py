import itertools
import sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from seer.core import split_class_n
from seer.outcome_memo import separated_pairs
from seer2.refine import Out,_h
from seer.pair_state import PairSeer
from seer2.schema import Schema

def outcome(rows,ordered=False):
 rows=list(rows);bag=_h(sorted(rows,key=repr))
 return Out('ok',bag,_h(rows) if ordered else None,rows,ordered=ordered)

def exact(pairs,outs):return [p for p in pairs if len(split_class_n(list(p),outs))==2]

def test_complete_result_memo_keeps_mixed_order_nontransitivity():
 rows=[(1,),(2,)]
 templates=[outcome(rows),outcome(rows,True),outcome(list(reversed(rows)),True)]
 outs={f'q{i:03}':templates[i%3] for i in range(120)}
 pairs=list(itertools.combinations(outs,2))
 result,stats=separated_pairs(pairs,outs)
 assert result==exact(pairs,outs)
 assert ('q001','q002') in result
 assert ('q000','q001') not in result and ('q000','q002') not in result
 assert stats['comparison_evaluations']<=9 and stats['comparison_cache_hits']>7000

@pytest.mark.parametrize('order',list(itertools.permutations(range(3))))
def test_float_tolerance_is_not_used_as_an_interning_relation(order):
 values=[0.,0.9e-6,1.8e-6]
 outs={f'q{i}':outcome([(values[index],)]) for i,index in enumerate(order)}
 pairs=list(itertools.combinations(outs,2))
 result,stats=separated_pairs(pairs,outs)
 assert result==exact(pairs,outs) and len(result)==1
 assert stats['comparison_outcome_groups']==3


def test_hash_collision_and_scalar_type_do_not_admit_incomplete_identity():
 outs={'a':outcome([(1,)]),'b':outcome([(2,)]),'c':outcome([(1.,)])}
 for out in outs.values():out.bag='deliberate_same_hash'
 result,stats=separated_pairs(list(itertools.combinations(outs,2)),outs)
 assert result==exact(list(itertools.combinations(outs,2)),outs)
 assert stats['comparison_outcome_groups']==3


def test_errors_null_nondeterminism_and_unknown_values_match_pair_kernel():
 outs={'a':outcome([(None,)]),'b':outcome([(1,)]),
       'err':Out('err',msg='timeout'),
       'n1':Out('nondet',('#n',1),None,[(1,)]),
       'n2':Out('nondet',('#n',2),None,[(1,),(2,)]),
       'nunknown':Out('nondet',None,None,None),
       'dict1':outcome([({'x':1},)]),'dict2':outcome([({'x':1},)])}
 pairs=list(itertools.combinations(outs,2))
 result,stats=separated_pairs(pairs,outs)
 assert result==exact(pairs,outs)
 assert all('err' not in p for p in result)


def test_lru_is_bounded_and_does_not_reuse_a_different_instance():
 outs={str(i):outcome([(i,)]) for i in range(25)}
 pairs=list(itertools.combinations(outs,2))
 result,stats=separated_pairs(pairs,outs,maxsize=2)
 assert result==exact(pairs,outs) and stats['comparison_cache_size']==2
 outs['0']=outs['1']
 result,stats=separated_pairs(pairs,outs,maxsize=2)
 assert result==exact(pairs,outs) and ('0','1') not in result

@pytest.mark.parametrize('order',list(itertools.permutations(range(3))))
def test_real_sql_certificate_and_pair_verdicts_match_original(order):
 queries=[('bridge','SELECT x FROM t'),('ascending','SELECT x FROM t ORDER BY x ASC'),
          ('descending','SELECT x FROM t ORDER BY x DESC')]
 queries=[queries[i] for i in order]
 def engine(memo):return PairSeer(Schema('CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);','sqlite'),
                    queries,dialect='sqlite',backend='sqlite',fast=False,budget=10,outcome_memo=memo)
 a,b=engine(False),engine(True);db={'t':[{'x':1},{'x':2}]}
 assert a.run_db(db,'native')==b.run_db(db,'native')==1
 a.recheck();b.recheck()
 assert a.dbs==b.dbs and a.events==b.events
 for x,y in itertools.combinations(dict(queries),2):assert a.verdict(x,y)==b.verdict(x,y)
 assert b.stats['comparison_pairs']==3
 assert b.stats['comparison_cache_peak']<=4096


def test_native_measurement_reuses_the_script_runner_from_a_module_entry():
    import subprocess
    result=subprocess.run([sys.executable,'-c','import scripts.measure_outcome_memo'],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
