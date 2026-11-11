from seer.stream import StreamPartition
from seer2.schema import Schema


def test_quick_counterexample_is_replayed_and_skips_polygon():
    order = []
    def prover(*args):
        order.append('sqlsolver')
        return {'status': 'not_proved'}
    def witness(*args):
        order.append('sqlwitness')
        return {'status': 'candidate', 'database': {'t': [{'x': None}]},
                'native': {'verdict': 'neq'}}
    s = StreamPartition(Schema('CREATE TABLE t(x INTEGER);', 'sqlite'),
                        random_dbs=0, budget_s=10, prover=prover,
                        quick_witness=witness)
    s.add('a','SELECT COUNT(*) FROM t')
    s.add('b','SELECT COUNT(x) FROM t')
    assert order == ['sqlsolver', 'sqlwitness']
    assert s.fingerprints['a'] != s.fingerprints['b']
    assert s.metrics['solver_calls'] == 0
    assert s.metrics['quick_witness_calls'] == s.metrics['quick_witness_admitted'] == 1
    assert s.assert_invariants()
    snap=s.snapshot()
    assert snap['class_sizes'] == [1, 1]
    assert snap['pair_counts'] == {'separated':1,'proved':0,'bounded':0,'undecided':0}
    assert snap['metrics']['first_fingerprint_separated_pair_fraction'] == 0


def test_absent_or_invalid_quick_witness_never_claims_equivalence():
    schema=Schema('CREATE TABLE t(x INTEGER NOT NULL);', 'sqlite')
    s=StreamPartition(schema,random_dbs=0,budget_s=10,bounds=(1,),
        quick_witness=lambda *args: {'status':'candidate',
            'database':{'t':[{'x':None}]},'native':{'verdict':'neq'}})
    s.add('a','SELECT x FROM t')
    s.add('b','SELECT x FROM t WHERE 1=1')
    assert s.metrics['quick_witness_admitted'] == 0
    assert s.metrics['rejected_candidates'] == 1
    assert s.relation('b')['status'] == 'bounded'
    assert s.metrics['solver_calls'] > 0
    assert s.assert_invariants()
    assert s.snapshot()['pair_counts']['bounded'] == 1
    s=StreamPartition(schema,random_dbs=0,budget_s=10,bounds=(1,),
        quick_witness=lambda *args: {'status':'no_witness',
            'native':{'verdict':'unknown'}})
    s.add('a','SELECT x FROM t')
    s.add('b','SELECT x FROM t WHERE 1=1')
    assert s.relation('b')['status'] == 'bounded'
    assert s.metrics['solver_calls'] > 0


def test_pairwise_and_no_pool_ablations_keep_partition_evidence():
    s=StreamPartition(Schema('CREATE TABLE t(x INTEGER);','sqlite'),
                      random_dbs=0,budget_s=10,bounds=(1,))
    s.add('a','SELECT x FROM t')
    s.add('b','SELECT x FROM t WHERE 1=1')
    assert s.relation('b')['status']=='bounded'
    previous=s.metrics['pair_decision_requests']
    s._compare('b','a',force=True)
    assert s.metrics['pair_decision_requests']==previous+1
    s.add('c','SELECT COUNT(*) FROM t')
    s.add('d','SELECT COUNT(x) FROM t')
    assert s._install({'t':[{'x':None}]},target=('c','d'))
    before=(len(s.witnesses),s.metrics['pair_decision_requests'])
    s.probe_without_pool('d','c')
    assert len(s.witnesses)==before[0]
    assert s.metrics['pair_decision_requests']==before[1]+1
    assert s.fingerprints['c']!=s.fingerprints['d']
    assert s.assert_invariants()
