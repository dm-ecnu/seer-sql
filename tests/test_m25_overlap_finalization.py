import copy,json,os,subprocess,sys
from pathlib import Path
from types import SimpleNamespace
from seer.stream import StreamPartition
from seer.stream_runner import run_stream
from seer2.schema import Schema

def obs(k):return SimpleNamespace(key=('rows',k),stable=True,cardinality=None)
def test_deactivated_column_changes_final_overlap_but_not_live_cache():
    def w(a,b,installed):return SimpleNamespace(active=True,mode='full',arrival_at_install=installed,observations={'a':obs(a),'b':obs(b)})
    seed,first,last=w(0,1,0),w(0,2,2),w(0,3,2)
    e=object.__new__(StreamPartition);e.arrival=['a','b'];e.witnesses=[seed,first,last];e.no_fingerprint_grouping=False
    e._witness_origin={id(seed):'seed',id(first):'quick',id(last):'quick'};e.metrics={}
    e._recompute_attribution();assert e._lib_overlap_count==2
    seed.active=False
    # This is the stale-cache mechanism seen in all five largest-batch traces.
    assert e._raw_library_overlaps()[0]==1 and e._lib_overlap_count==2
    before=[(w.active,w.mode,dict(w.observations)) for w in e.witnesses]
    e._recompute_attribution();assert e._lib_overlap_count==1
    assert e._lib_overlap_pair=={frozenset(('a','b')):'quick'}
    assert [(w.active,w.mode,dict(w.observations)) for w in e.witnesses]==before
    e._recompute_attribution();assert e._lib_overlap_count==1

def test_finalization_repairs_only_meter_after_decisions(monkeypatch,tmp_path):
    schema=Schema('CREATE TABLE t(x INTEGER);','sqlite')
    queries=[('a','SELECT 1'),('b','SELECT 2'),('c','SELECT 3')]
    kwargs=dict(schema=schema,queries=queries,dialect='sqlite',backend='sqlite',budget_s=30,seed=0,
        native_runtime=tmp_path/'unused.json',evidence_dir=tmp_path/'evidence',enable_prover=False,
        quick_sqlwitness=False,schedule='tail_proof',random_dbs=0)
    old,result=run_stream(**kwargs)
    real_add=StreamPartition.add
    def add_stale(self,q,sql,*args,**kw):
        ok=real_add(self,q,sql,*args,**kw)
        if q=='c':
            self._lib_overlap_count=777
            self._lib_overlap_pair={frozenset(('a','b')):'quick'}
        return ok
    monkeypatch.setattr(StreamPartition,'add',add_stale)
    new,fixed=run_stream(**kwargs)
    assert fixed['pair_counts']==result['pair_counts']
    assert fixed['classes']==result['classes']
    assert fixed['relations']==result['relations']
    assert new.fingerprints==old.fingerprints
    assert new._lib_overlap_count==old._lib_overlap_count==0
    for key in ('solver_calls','prover_calls','quick_witness_calls','pair_decision_requests','library_overlap_raw_events','library_arrival_fingerprint_overlap_pairs'):
        assert fixed['metrics'][key]==result['metrics'][key]
    assert new.assert_invariants()

def test_cli_keeps_stderr_inside_failed_receipt(tmp_path):
    tree=Path(__file__).resolve().parents[1];source=tmp_path/'empty.jsonl';source.write_text('')
    out=tmp_path/'failed-receipt'
    p=subprocess.run([sys.executable,str(tree/'scripts/run_stream_batch_takeover_v2.py'),
        '--dataset',str(source),'--batch','not-present','--method','seer_stream_sqlwitness','--out',str(out)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=dict(os.environ,PYTHONUNBUFFERED='1'))
    assert p.returncode==1
    assert 'expected exactly one batch' in (out/'stderr.log').read_text()
    assert (out/'stdout.log').exists()
