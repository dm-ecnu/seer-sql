import pytest
from seer.stream import StreamPartition, Observation
from seer2.schema import Schema

def engine():
    s=StreamPartition(Schema('CREATE TABLE t(x INTEGER);','sqlite'),random_dbs=0,budget_s=30,bounds=(1,),quick_witness=lambda *a:{'database':{'t':[{'x':1},{'x':2}]},'status':'candidate'})
    s.add('a','SELECT x FROM t',compare=False)
    s.add('b','SELECT x+1 FROM t',compare=False)
    s._compare('b',rep='a',refutation_only=True)
    assert s._quick_admitted[('b','a')]
    return s

def test_full_to_cardinality_revokes_only_live_quick_ledger():
    s=engine();before=s.metrics['quick_witness_admitted']
    s.add('unstable','SELECT x FROM t ORDER BY x%1',compare=False)
    assert s.witnesses[-1].mode=='cardinality'
    assert s.fingerprints['a']==s.fingerprints['b']
    assert s._quick_admitted[('b','a')] is False
    assert s.metrics['quick_witness_admitted']==before
    assert s.metrics['quick_admissions_revoked']==1
    assert any(e['event']=='quick_witness' and e['engine_admitted'] for e in s.events)
    assert any(e['event']=='quick_admission_revoked' for e in s.events)
    s._recompute_attribution()
    assert s.assert_invariants()
    assert s.snapshot()['pair_counts']['separated']==2

def test_resource_unresolved_column_is_removed_not_used_as_a_wildcard(monkeypatch):
    s=engine();real=s._run
    def run(db,qs,phase='pool_execution_s'):
        if qs==['unresolved']:
            return {'unresolved':Observation(('bag','error','interrupted'),False,'execution_resource_limit',None)}
        return real(db,qs,phase)
    monkeypatch.setattr(s,'_run',run)
    s.add('unresolved','SELECT 0',compare=False)
    assert not any(w.active for w in s.witnesses)
    assert s._quick_admitted[('b','a')] is False
    assert len(s.index)==1
    s._recompute_attribution()
    assert s.assert_invariants()
    assert s.snapshot()['pair_counts']['separated']==0

def test_corrupt_fingerprints_or_disappeared_columns_still_fail():
    s=engine()
    s.fingerprints['b']=s.fingerprints['a']
    with pytest.raises(AssertionError):s.assert_quick_separation()
    s.witnesses=[]
    s.pending_reason['b']=('undecided','test')
    with pytest.raises(AssertionError,match='admitted quick library'):s.assert_quick_separation()
