"""The common shell must reach native calls before its budget on 500 SQLs."""
import sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'eval'))
from seereval import pair_shell

def test_500_queries_reach_native_and_keep_complete_pair_population(monkeypatch):
    queries=[{'qid':str(i),'sql':'SELECT '+str(i)} for i in range(500)]
    calls=[]
    monkeypatch.setattr(pair_shell,'empty_db_errors',lambda *a,**kw:{})
    def native(*args,**kwargs):
        calls.append(tuple(args[2]));return {'verdict':'unsupported','reason':'spec sentinel'}
    monkeypatch.setattr(pair_shell,'_tool_call',native)
    start=time.monotonic()
    result=pair_shell.pairwise_graph_shell('sqlwitness',None,'sqlite',queries,2,1,0,False)
    elapsed=time.monotonic()-start
    assert calls,'budget was spent in common scheduling before any native API'
    assert calls[0]==('0','1')
    assert len(result['pairs'])==124750
    assert len({frozenset((p['a'],p['b'])) for p in result['pairs']})==124750
    assert result['n_calls']==len(calls) and result['n_genuine_pairs']==0
    assert all(p['certificate'] is None for p in result['pairs'])
    assert elapsed<6

def test_round_snapshot_preserves_native_pair_order_and_error_exclusion(monkeypatch):
    queries=[{'qid':str(i),'sql':'SELECT '+str(i)} for i in range(4)]
    monkeypatch.setattr(pair_shell,'empty_db_errors',lambda *a,**kw:{'3':'spec query error'})
    calls=[]
    def native(*args,**kwargs):
        calls.append(tuple(args[2]));return {'verdict':'unsupported'}
    monkeypatch.setattr(pair_shell,'_tool_call',native)
    result=pair_shell.pairwise_graph_shell('parseval',None,'sqlite',queries,5,1,0,True)
    assert calls==[('0','1'),('0','2'),('1','2')]
    assert len(result['pairs'])==6 and result['error_class']==['3']
    assert sum(p.get('reason')=='query_error' for p in result['pairs'])==3
