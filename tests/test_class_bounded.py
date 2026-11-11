import time
import pytest
import z3
from seer2.schema import Schema
from seer.representative_search import RepresentativeSearch
from seer.class_search import ClassContext, ClassWorker
from seer.stream import StreamPartition
from seer.stream_class import prove_classes


def search(ddl='CREATE TABLE t(x INTEGER);', queries=None):
    s=RepresentativeSearch(Schema(ddl,'sqlite'),'sqlite')
    for q,sql in (queries or {'a':'SELECT x FROM t','b':'SELECT x FROM t WHERE 1=1'}).items():s.add_query(q,sql)
    return s


def test_one_context_shared_base_and_representative_across_capacity_and_chunks():
    s=search(queries={'a':'SELECT x FROM t','b':'SELECT x FROM t WHERE 1=1','c':'SELECT x FROM t WHERE TRUE'})
    c=ClassContext(s,'a'); solver=c.solver
    assert c.check(['b'],1,5)['bounded_k']==1
    old=c.levels[1]; row=old.enc.base['t'][0]; output=old.out['a']
    assert c.check(['b'],2,5)['bounded_k']==2
    assert c.solver is solver and c.levels[2].solver is solver
    assert c.levels[2].enc.base['t'][0] is row
    assert c.check(['c'],1,5)['bounded_k']==1
    assert c.levels[1] is old and old.out['a'] is output
    assert c.solver.num_scopes()==0


def test_shallow_fk_does_not_restrict_deeper_capacity():
    s=search('CREATE TABLE p(x INTEGER PRIMARY KEY); CREATE TABLE t(x INTEGER REFERENCES p(x));',
             {'a':'SELECT x FROM t','b':'SELECT x FROM t WHERE 1=1'})
    c=ClassContext(s,'a')
    assert c.check(['b'],1,5)['bounded_k']==1
    assert c.check(['b'],2,5)['bounded_k']==2
    lv=c.levels[2]; base=lv.enc.base
    # First child points to the second parent: legal at k=2 but impossible
    # under the retained, INACTIVE k=1 FK constraint.
    goal=z3.And(base['t'][0][0],base['p'][1][0],
                base['t'][0][1][0][0]==2,base['p'][0][1][0][0]==1,
                base['p'][1][1][0][0]==2)
    result,model=lv.check(goal)
    assert result==z3.sat
    assert not s.schema.violations(lv.decode(model,s.schema))


def test_single_member_matches_individual_same_k():
    s=search(); c=ClassContext(s,'a')
    for k in (1,2,4):
        result=c.check(['b'],k,5)
        old=s._search_inner('b','a',5,bounds=(k,),canonical_contract=True,augment=0)
        assert result['bounded_k']==old['bounded_k']==k


def test_class_split_moves_members_outside_chunk_and_repeats_same_depth():
    e=StreamPartition(Schema('CREATE TABLE t(x INTEGER);','sqlite'),random_dbs=0,budget_s=30,bounds=(1,2))
    e.add('a','SELECT COUNT(*) FROM t',compare=False)
    for i in range(40):e.add(f'b{i}', 'SELECT COUNT(x) FROM t',compare=False)
    prove_classes(e)
    assert e.metrics['class_sat_splits']==1
    assert all(e.fingerprints[f'b{i}']!=e.fingerprints['a'] for i in range(40))
    assert any(x['event']=='class_split_validated' and len(x['moved'])==40 for x in e.events)
    assert e.assert_invariants(replay=True)


def test_cost_guard_halves_before_encoding():
    s=search(queries={'a':'SELECT x FROM t','b':'SELECT x FROM t','c':'SELECT x FROM t'})
    s.max_cost=2
    c=ClassContext(s,'a')
    r=c.check(['b','c'],1,5)
    assert r['status']=='reduce_chunk' and not c.levels


def test_deep_timeout_keeps_prior_labels_and_never_restarts(monkeypatch):
    from seer.class_search import ClassDepth
    original=ClassDepth.__init__
    def delayed(self,context,k,previous,deadline):
        if k==2:time.sleep(5)
        original(self,context,k,previous,deadline)
    monkeypatch.setattr(ClassDepth,'__init__',delayed)
    e=StreamPartition(Schema('CREATE TABLE t(x INTEGER);','sqlite'),random_dbs=0,budget_s=10,per_call_s=.6)
    e.add('a','SELECT x FROM t',compare=False);e.add('b','SELECT x FROM t WHERE TRUE',compare=False)
    prove_classes(e)
    assert e.relation('b')['status']=='bounded'
    assert e.relation('b')['k']==1
    assert e.metrics['class_contexts']==1


def test_runner_keeps_tiers_and_no_arrival_polygon(tmp_path):
    from seer.stream_runner import run_stream
    e,r=run_stream(Schema('CREATE TABLE t(x INTEGER);','sqlite'),
        [('a','SELECT x FROM t'),('b','SELECT x FROM t WHERE TRUE')],
        'sqlite','sqlite',20,0,None,tmp_path/'native',schedule='class_bounded',
        enable_prover=False,random_dbs=0,bounds=(1,2))
    assert r['tiers']['proof_wall_s']>0 and r['tiers']['separation_wall_s']>0
    end=next(i for i,x in enumerate(e.events) if x['event']=='stream_arrivals_complete')
    assert all(x['event'] not in ('representative_search','class_bounded_search') for x in e.events[:end])
    assert r['pair_counts']['bounded']==1
    assert e.relation('b')['k']==2


def test_schema_unsupported_is_a_verdict_not_a_worker_failure():
    from seer.class_validation import verify_singletons
    s=search('CREATE TABLE p(x INTEGER PRIMARY KEY); CREATE TABLE t(x VARCHAR(10) REFERENCES p(x));',
             {'a':'SELECT x FROM t','b':'SELECT x FROM t WHERE TRUE'})
    c=ClassContext(s,'a')
    assert c.check(['b'],1,5)['status']=='unsupported'
    records=verify_singletons(s,[('a','b')],bounds=(1,),seconds=5)
    assert records[0]['status']=='unsupported'


def test_runner_assertion_rejects_injected_false_certificate():
    from seer.class_validation import verify_bounded
    from seer.stream import Edge
    e=StreamPartition(Schema('CREATE TABLE t(x INTEGER);','sqlite'),random_dbs=0,budget_s=10)
    e.add('a','SELECT COUNT(*) FROM t',compare=False)
    e.add('b','SELECT COUNT(x) FROM t',compare=False)
    e.edges.append(Edge('a','b','bounded',{},1,e.search.domain_identity()))
    with pytest.raises(AssertionError):verify_bounded(e,seconds=5)


def test_microbatch_uses_frozen_yaml_path_adapter(tmp_path):
    from seer.official_batch import materialize
    from types import SimpleNamespace
    text='row_dep: unique(t.x)\ncol_val: t.x>=0\n'
    schema=SimpleNamespace(native={'sqlwitness':{'tables':[], 'constraints':text}},tables=[])
    benchmark,_=materialize(tmp_path/'input',schema,'sqlite',[('a','b','SELECT 1','SELECT 2')])
    assert benchmark=='bird'
    path=(tmp_path/'input/benchmarks/bird/constraints/batch').read_text()
    from pathlib import Path
    assert path.endswith('.yml') and Path(path).read_text()==text


def test_class_metric_keeps_pair_unit_not_chunk_or_depth_unit():
    e=StreamPartition(Schema('CREATE TABLE t(x INTEGER);','sqlite'),random_dbs=0,budget_s=10,bounds=(1,2))
    for q in ('a','b','c','d'):e.add(q,'SELECT x FROM t',compare=False)
    prove_classes(e)
    assert e.metrics['pair_decision_requests']==3
    assert e.metrics['class_pair_requests']==3
    assert e.metrics['class_checks']==2


def test_class_preserves_native_proof_and_admission():
    import hashlib
    e=StreamPartition(Schema('CREATE TABLE t(x INTEGER);','sqlite'),random_dbs=0,budget_s=10,bounds=(1,))
    for q in ('a','b'):e.add(q,'SELECT x FROM t',compare=False)
    e.prover=lambda a,b,budget:{'status':'native_eq','admission':{
        'q1_sha256':hashlib.sha256(a.encode()).hexdigest(),
        'q2_sha256':hashlib.sha256(b.encode()).hexdigest()}}
    e.prover_admit=lambda q,rep,receipt:True
    prove_classes(e)
    assert e.relation('b','a')['status']=='proved'
    assert e.metrics['prover_calls']==1 and e.metrics['class_checks']==0
