"""SMT XOR proofs and native acceptance checks of guarded goal compression."""
import itertools,sys,time
from pathlib import Path
from types import SimpleNamespace
import pytest,z3
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from seer.pair_state import PairSeer
from seer.symbolic import Level
from seer.encode import Domain,Rel,schema_strings
from seer.goal_graph import select_pairs
from seer2.schema import Schema
DDL='CREATE TABLE t(x INTEGER);'
SQL=['SELECT x FROM t','SELECT x FROM t WHERE x>=0','SELECT x FROM t WHERE x<=0','SELECT x FROM t WHERE x=0']

def engine(sql=SQL,**kwargs):
    return PairSeer(Schema(DDL,'sqlite'),[(str(i),q) for i,q in enumerate(sql)],dialect='sqlite',backend='sqlite',
                    fast=False,levels=(1,2),max_k=2,budget=10,**kwargs)

def level(sql=SQL,k=2):
    s=engine(sql)
    lv=Level(s.S,s.front,Domain.build(list(s.ir.values()),checks_strings=schema_strings(s.S)),s.ir,k,'sqlite',timeout_ms=3000)
    assert set(lv.out)==set(s.ir),lv.unsup
    return lv

def goal(lv,pairs):
    return z3.Or([z3.And(lv.valid(p),lv.sep(*p)) for p in pairs])

def equivalent(lv,original,selected):
    assert lv.check(z3.Xor(goal(lv,original),goal(lv,selected)))[0]==z3.unsat


def test_nullable_bag_goals_are_pointwise_equivalent_after_forest_selection():
    lv=level();pairs=list(itertools.combinations(lv.out,2))
    selected,info=select_pairs(lv,pairs,'safe_forest')
    assert len(pairs)==6 and len(selected)==3
    assert {tuple(p) for p in selected}<=set(pairs)
    assert info['omitted_pairs']==3 and info['eligible_pairs']==6
    equivalent(lv,pairs,selected)


def test_residual_cycle_needs_a_forest_not_an_arbitrary_global_star():
    lv=level();pairs=[('0','1'),('1','2'),('2','3'),('0','3')]
    selected,info=select_pairs(lv,pairs,'safe_forest')
    assert selected==pairs[:3] and info['omitted_pairs']==1
    equivalent(lv,pairs,selected)


def test_disconnected_residual_population_never_invents_cross_component_edges():
    lv=level(SQL+['SELECT x+1 FROM t'])
    pairs=[('0','1'),('1','2'),('0','2'),('3','4')]
    selected,_=select_pairs(lv,pairs,'safe_forest')
    assert selected==[pairs[0],pairs[1],pairs[3]]
    equivalent(lv,pairs,selected)


def manual(ordered=True):
    lv=level(['SELECT x FROM t']*3)
    def output(values,positions,det):
        return Rel([(z3.BoolVal(True),[(z3.IntVal(x),z3.BoolVal(False))]) for x in values],('int',),
                   None if positions is None else list(map(z3.IntVal,positions)),z3.BoolVal(det))
    lv.out={'a':output([1,2],[0,1] if ordered else None,True),
            'b':output([1,2],[0,1] if ordered else None,True),
            'c':output([2,1],[0,1] if ordered else None,True)}
    lv.err={q:z3.BoolVal(False) for q in lv.out}
    lv.irs={q:SimpleNamespace(op='sort' if ordered else 'scan') for q in lv.out}
    return lv


def test_determinate_ordered_relations_have_a_separate_forest():
    lv=manual();pairs=[('a','b'),('b','c'),('a','c')]
    selected,info=select_pairs(lv,pairs,'safe_forest')
    assert len(selected)==2 and info['relation_groups']==1
    equivalent(lv,pairs,selected)


def test_dynamic_determinacy_bridge_cannot_hide_order_difference():
    lv=manual();lv.out['b'].det=z3.BoolVal(False)
    pairs=[('a','b'),('b','c'),('a','c')]
    assert lv.check(z3.And(goal(lv,pairs),z3.Not(goal(lv,pairs[:2]))))[0]==z3.sat
    selected,info=select_pairs(lv,pairs,'safe_forest')
    assert selected==pairs and info['relation_groups']==2
    equivalent(lv,pairs,selected)


def test_unordered_bridge_cannot_hide_ordered_endpoints():
    lv=manual();lv.out['b'].pos=None
    pairs=[('a','b'),('b','c'),('a','c')]
    assert lv.check(z3.And(goal(lv,pairs),z3.Not(goal(lv,pairs[:2]))))[0]==z3.sat
    selected,info=select_pairs(lv,pairs,'safe_forest')
    assert selected==pairs and info['relation_groups']==2
    equivalent(lv,pairs,selected)


def test_runtime_error_bridge_is_not_used_for_transitive_compression():
    lv=manual(False);lv.out['c'].rows=[(z3.BoolVal(True),[(z3.IntVal(9),z3.BoolVal(False))])]
    lv.err['b']=z3.Bool('middle_runtime_error')
    pairs=[('a','b'),('b','c'),('a','c')]
    assert lv.check(z3.And(lv.err['b'],goal(lv,pairs),z3.Not(goal(lv,pairs[:2]))))[0]==z3.sat
    selected,info=select_pairs(lv,pairs,'safe_forest')
    assert selected==pairs and info['protection_reasons']['runtime_validity']==2
    equivalent(lv,pairs,selected)


def test_sparse_position_bridge_is_protected_even_when_det_is_true():
    lv=manual();lv.out['b'].pos=[z3.IntVal(2),z3.IntVal(3)];lv.irs['b']=SimpleNamespace(op='project')
    pairs=[('a','b'),('b','c'),('a','c')]
    assert lv.check(z3.And(goal(lv,pairs),z3.Not(goal(lv,pairs[:2]))))[0]==z3.sat
    selected,info=select_pairs(lv,pairs,'safe_forest')
    assert selected==pairs and info['protection_reasons']['order_or_position_contract']==2
    equivalent(lv,pairs,selected)


def test_different_output_types_are_kept_as_original_pair_goals():
    lv=manual(False);lv.out['b'].types=('string',)
    pairs=[('a','b'),('b','c'),('a','c')]
    selected,info=select_pairs(lv,pairs,'safe_forest')
    assert selected==pairs and info['protection_reasons']['output_types']==2
    equivalent(lv,pairs,selected)


@pytest.mark.parametrize('suffix',['',' LIMIT 1',' LIMIT 2 OFFSET 1',' LIMIT 0'])
def test_actual_top_sort_produces_dense_unique_live_positions(suffix):
    lv=level(['SELECT x FROM t ORDER BY x'+suffix]*3,k=3)
    out=lv.out['0'];assert lv.irs['0'].op=='sort'
    count=z3.Sum([z3.If(e,1,0) for e,_ in out.rows]);bad=[]
    for i,((active,_),position) in enumerate(zip(out.rows,out.pos)):
        bad.append(z3.And(active,z3.Or(position<0,position>=count)))
        for j in range(i):bad.append(z3.And(active,out.rows[j][0],position==out.pos[j]))
    assert lv.check(z3.Or(bad))[0]==z3.unsat


def test_actual_single_row_sort_can_be_compressed_when_det_simplifies_true():
    lv=level(['SELECT x FROM t ORDER BY x']*4,k=1);pairs=list(itertools.combinations(lv.out,2))
    selected,info=select_pairs(lv,pairs,'safe_forest')
    assert len(selected)==3 and info['protected_pairs']==0
    equivalent(lv,pairs,selected)


def test_bounded_unsat_still_proves_all_original_pairs():
    s=engine(['SELECT x FROM t']*5,goal_policy='safe_forest');result=s.run()
    assert all(s.verdict(*p)=={'verdict':'bounded_equivalent','k':2} for p in itertools.combinations(s.queries,2))
    assert result['stats']['symbolic_goal_pairs_omitted']>0 and result['certificates']['dbs']==[]


def test_blocked_unconfirmed_models_cannot_turn_into_equivalence(monkeypatch):
    s=engine(['SELECT x FROM t','SELECT x+1 FROM t','SELECT x+2 FROM t'],goal_policy='safe_forest')
    # The injected restricted UNSAT is scoped to one solver level.
    s.levels=(2,)
    original=Level.find;calls=[]
    def find(self,*args,**kwargs):
        calls.append(1)
        return original(self,*args,**kwargs) if len(calls)==1 else ('unsat',None,None)
    monkeypatch.setattr(Level,'find',find);monkeypatch.setattr(s,'run_db',lambda *args,**kwargs:0)
    result=s.run()
    assert result['stats'].get('bounded_proof_withheld',0)>0
    assert all(s.verdict(*p)['verdict']=='undecided' for p in itertools.combinations(s.queries,2))


def test_forest_does_not_transitively_close_native_approximate_equality():
    sql=['SELECT CAST(0.00000075 AS REAL)','SELECT CAST(0 AS REAL)','SELECT CAST(0.0000015 AS REAL)']
    s=engine(sql,goal_policy='safe_forest');s.run()
    assert s.verdict('1','2')['verdict']=='split'
    assert s.verdict('0','1')['verdict']!='split' and s.verdict('0','2')['verdict']!='split'


def test_max_pairs_objective_cannot_silently_use_a_feasibility_forest():
    with pytest.raises(ValueError):engine(goal_policy='safe_forest',symbolic_selection='max_pairs')


def test_deadline_during_selection_is_not_an_empty_unsat_goal():
    lv=level();lv.deadline=time.perf_counter()-1
    with pytest.raises(TimeoutError):select_pairs(lv,list(itertools.combinations(lv.out,2)),'safe_forest')


def test_original_all_pairs_policy_keeps_exact_order_and_population():
    lv=level();pairs=list(itertools.combinations(lv.out,2));selected,info=select_pairs(lv,pairs,'all_pairs')
    assert selected is pairs and info['selected_pairs']==len(pairs) and not info['eligibility_evaluated']



def test_goal_policy_is_independent_of_other_three_component_switches():
    from hard_deadline import resolve_goal_policy,ablation_config
    for name in ('full','no_hard_sigma','no_deterministic_query_memo','no_cross_pair_certificate_reuse'):
        assert resolve_goal_policy(name,'safe_forest')=='safe_forest'
    assert not ablation_config('pair_seer','no_hard_sigma')['encode_sigma']
    assert not ablation_config('pair_seer','no_deterministic_query_memo')['share_query_encoding']


def trace_row():
    from seer.goal_graph import dependency_path,WHEEL_SHA256
    info={'kind':'symbolic_goal_selection_built','k':1,'policy':'safe_forest','input_pairs':6,
          'selected_pairs':3,'omitted_pairs':3,'selection_s':0.01,'eligibility_evaluated':True,
          'eligible_pairs':6,'protected_pairs':0,'eligible_vertices':4,'eligible_components':1,'forest_edges':3,
          'protection_reasons':{},'dependency_sha256':WHEEL_SHA256}
    request={'kind':'encoding_target_request','k':1,'groups':3,'pair_goals':3,'max_group_size':2}
    config={'share_query_encoding':True,'encode_sigma':True}
    return {'goal_policy':'safe_forest','comparison_contract':{'goal_policy':'safe_forest','encoding_configuration':config,
        'witness_reuse':True,'files':{str(dependency_path()):WHEEL_SHA256}},'encoding_configuration':config,
        'witness_reuse':True,'call_metrics':{'entries':[info,request]}}


def test_scope_reader_checks_targets_used_by_the_actual_solver():
    from check_goal_forest_unit import verify_goal_trace
    row=trace_row();assert verify_goal_trace(row)['omitted_pairs_over_completed_rounds']==3
    row['call_metrics']['entries'][1]['groups']=6
    with pytest.raises(AssertionError):verify_goal_trace(row)


def test_scope_reader_rejects_deleting_protected_goals():
    from check_goal_forest_unit import verify_goal_trace
    row=trace_row();row['call_metrics']['entries'][0].update({'eligible_pairs':2,'protected_pairs':4,'protection_reasons':{'runtime_validity':4}})
    with pytest.raises(AssertionError):verify_goal_trace(row)


def test_scope_reader_rejects_an_unpinned_graph_dependency():
    from check_goal_forest_unit import verify_goal_trace
    row=trace_row();row['call_metrics']['entries'][0]['dependency_sha256']='unknown'
    with pytest.raises(AssertionError):verify_goal_trace(row)


def test_scope_reader_rejects_a_nonspanning_selected_structure():
    from check_goal_forest_unit import verify_goal_trace
    row=trace_row();row['call_metrics']['entries'][0]['eligible_components']=2
    with pytest.raises(AssertionError):verify_goal_trace(row)
