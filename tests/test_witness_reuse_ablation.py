"""Real DBMS checks of single-target certificate acceptance, not synthetic scores."""
import itertools,json,sys,time
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'eval'),str(ROOT/'scripts')]
from seer.pair_state import PairSeer
from seer.core import replay
from seer2.schema import Schema
from seereval.schema import from_ddl
from hard_deadline import Collector,ablation_config
DDL='CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);'
DB={'t':[{'x':1},{'x':2}]}
QUERIES=[('a','SELECT x FROM t'),('b','SELECT x+1 FROM t'),('c','SELECT x+2 FROM t')]

def engine(reuse,queries=QUERIES,symbolic=False):
    return PairSeer(Schema(DDL,'sqlite'),queries,dialect='sqlite',backend='sqlite',fast=False,
                    symbolic=symbolic,levels=(1,2),max_k=2,budget=10,witness_reuse=reuse,shared_pair_replay=True)


class CountingBackend:
    def __init__(self,wrapped):self.wrapped=wrapped;self.loads=0
    def __getattr__(self,name):return getattr(self.wrapped,name)
    def open(self,*args,**kwargs):self.loads+=1;return self.wrapped.open(*args,**kwargs)


def test_one_new_proposal_has_one_pair_and_two_query_executions_when_reuse_disabled():
    full,local=engine(True),engine(False)
    a,b=full.stats['executions'],local.stats['executions']
    assert full.run_db(DB,'spec')==3
    assert local.run_db(DB,'spec')==1
    assert full.stats['executions']-a==3
    assert local.stats['executions']-b==2
    assert len(local.dbs)==len(local.events)==1
    assert local.verdict('a','b')['verdict']=='split'
    assert local.verdict('a','c')['verdict']=='undecided'
    assert local.verdict('b','c')['verdict']=='undecided'
    assert not local.shared_pair_replay


def test_new_proposals_rebuild_and_reexecute_even_if_database_content_repeats():
    local=engine(False);local.backend=CountingBackend(local.backend)
    for _ in range(3):assert local.run_db(DB,'new_proposal')==1
    assert local.backend.loads==3 and len(local.dbs)==len(local.events)==3
    assert all(len(pair)==2 for _,pair,_ in local.events)
    assert {idx for idx,_,_ in local.events}==set(range(3))
    assert all(local.verdict(*p)['verdict']=='split' for p in itertools.combinations('abc',2))
    assert replay(local.S,local.result(),dialect='sqlite',backend='sqlite')==3


def test_equal_first_pair_does_not_starve_other_targets():
    local=engine(False,[('a','SELECT x FROM t'),('b','SELECT x FROM t'),('c','SELECT x+1 FROM t')])
    assert local.run_db(DB,'proposal')==0
    for _ in range(3):local.run_db(DB,'proposal')
    assert local.verdict('a','b')['verdict']=='undecided'
    assert local.verdict('a','c')['verdict']==local.verdict('b','c')['verdict']=='split'
    assert local.stats['witness_proposals']==4


def test_symbolic_predictions_supply_focal_pairs_without_dropping_other_actual_edges():
    local=engine(False,symbolic=True);full=engine(True,symbolic=True)
    local_result=local.run();full_result=full.run()
    assert all(local.verdict(*p)['verdict']==full.verdict(*p)['verdict']=='split' for p in itertools.combinations('abc',2))
    assert len(local_result['certificates']['dbs'])==3
    assert len(full_result['certificates']['dbs'])==1
    assert len(local.events)==3 and len({idx for idx,_,_ in local.events})==3
    assert local_result['stats'].get('model_mismatch',0)==0


def test_mixed_bag_list_contract_is_still_pair_local():
    qs=[('bridge','SELECT x FROM t'),('ascending','SELECT x FROM t ORDER BY x'),('descending','SELECT x FROM t ORDER BY x DESC')]
    local=engine(False,qs,symbolic=True);result=local.run()
    assert local.verdict('ascending','descending')['verdict']=='split'
    assert local.verdict('bridge','ascending')['verdict']=='bounded_equivalent'
    assert local.verdict('bridge','descending')['verdict']=='bounded_equivalent'
    assert replay(local.S,result,dialect='sqlite',backend='sqlite')==1


def test_incremental_query_does_not_reuse_stored_database_when_disabled(monkeypatch):
    local=engine(False,QUERIES[:2]);assert local.run_db(DB,'proposal')==1
    monkeypatch.setattr(local,'run',lambda:local.result())
    local.add_query(*QUERIES[2])
    assert len(local.dbs)==1 and local.stats['witness_proposals']==1
    assert local.verdict('a','b')['verdict']=='split'
    assert local.verdict('a','c')['verdict']==local.verdict('b','c')['verdict']=='undecided'


class Connection:
    def __init__(self):self.notices=[]
    def send(self,notice):self.notices.append(notice)


def collector(tmp_path,reuse,queries=QUERIES):
    (tmp_path/'schema.json').write_text(json.dumps(from_ddl('scope',DDL,dialect='sqlite').to_json()))
    batch={'batch_id':'scope','dataset':'spec','dialect':'sqlite','schema_path':'schema.json',
           'queries':[{'qid':q,'sql':sql} for q,sql in queries]}
    return Collector(batch,str(tmp_path),time.monotonic()+10,Connection(),tmp_path,allow_cross_pair_reuse=reuse)


def certificates(tmp_path):
    return [r for p in sorted(tmp_path.glob('event-*.json')) if (r:=json.loads(p.read_text()))['kind']=='certificate']


def test_parent_validates_only_target_pair_and_never_adds_cross_pair_credit(tmp_path,monkeypatch):
    c=collector(tmp_path,False)
    from seereval import validator
    original=validator.check_db;observed=[]
    def actual(*args,**kwargs):
        observed.append(set(args[3]));return original(*args,**kwargs)
    monkeypatch.setattr(validator,'check_db',actual)
    c.offer(DB,target_pair=('a','b'))
    assert observed==[{'a','b'},{'a','b'}]
    events=certificates(tmp_path)
    assert len(events)==1 and events[0]['pairs']==[['a','b']]
    assert events[0]['target_pair']==['a','b']
    assert c.accepted=={frozenset(('a','b'))}


def test_parent_requires_explicit_target_in_disabled_mode(tmp_path):
    c=collector(tmp_path,False)
    with pytest.raises(ValueError):c.offer(DB)
    assert not certificates(tmp_path)


def test_parent_does_not_credit_a_different_pair_when_target_is_equal(tmp_path):
    c=collector(tmp_path,False,[('a','SELECT x FROM t'),('b','SELECT x FROM t'),('c','SELECT x+1 FROM t')])
    c.offer(DB,target_pair=('a','b'))
    assert c.accepted==set() and not certificates(tmp_path)


def test_parent_rejects_illegal_and_late_single_pair_databases(tmp_path):
    c=collector(tmp_path,False)
    c.offer({'t':[{'x':None}]},target_pair=('a','b'))
    assert c.accepted==set()
    c.deadline=time.monotonic()-1;c.offer(DB,target_pair=('a','b'))
    assert c.accepted==set() and not certificates(tmp_path)


def test_default_parent_still_expands_the_whole_batch(tmp_path):
    c=collector(tmp_path,True);c.offer(DB)
    assert len(c.accepted)==3
    assert sum(len(e['pairs']) for e in certificates(tmp_path))==3


def test_witness_switch_does_not_remove_encoding_or_sigma():
    assert ablation_config('pair_seer','no_cross_pair_certificate_reuse')==ablation_config('pair_seer','full')
    with pytest.raises(ValueError):ablation_config('sqlwitness','no_cross_pair_certificate_reuse')



def scope_row(tmp_path):
    import hashlib
    target=['a','b']
    event={'kind':'certificate','pairs':[target],'target_pair':target,'proposal_id':0}
    path=tmp_path/'event-000000.json';path.write_text(json.dumps(event))
    return {'ablation':'no_cross_pair_certificate_reuse','witness_reuse':False,
        'comparison_contract':{'witness_reuse':False},'encoding_configuration':{'share_query_encoding':True,'encode_sigma':True},
        'journal':str(tmp_path),'call_metrics':{'entries':[
            {'kind':'witness_proposal_scope','proposal_id':0,'cross_pair_certificate_reuse':False,'pair_count':1,'target_pair':target,'t':1},
            {'kind':'witness_proposal_finished','proposal_id':0,'cross_pair_certificate_reuse':False,'gain':1,'new_database_count':1,'source_query_evaluations':2,'order_probe_evaluations':0,'t':2},
            {'kind':'witness_validation_scope','proposal_id':0,'cross_pair_reuse':False,'query_count':2,'target_pair':target,'t':3}]},
        'accepted_receipts':[{'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}],
        'pairs':[{'a':'a','b':'b','certificate':0,'verdict':'split'}],'dbs':[DB],'n_genuine_pairs':1}


def test_scope_reader_rejects_parent_expansion(tmp_path):
    from check_witness_ablation_unit import verify_witness_scope
    row=scope_row(tmp_path);verify_witness_scope(row)
    row['call_metrics']['entries'][-1]['query_count']=3
    with pytest.raises(AssertionError):verify_witness_scope(row)


def test_scope_reader_rejects_extra_native_executions(tmp_path):
    from check_witness_ablation_unit import verify_witness_scope
    row=scope_row(tmp_path);row['call_metrics']['entries'][1]['source_query_evaluations']=3
    with pytest.raises(AssertionError):verify_witness_scope(row)


def test_scope_reader_rejects_other_pair_credit_from_single_proposal(tmp_path):
    from check_witness_ablation_unit import verify_witness_scope
    row=scope_row(tmp_path);row['pairs'].append({'a':'a','b':'c','certificate':0,'verdict':'split'});row['n_genuine_pairs']=2
    with pytest.raises(AssertionError):verify_witness_scope(row)


def test_scope_reader_rejects_target_selected_after_validation(tmp_path):
    from check_witness_ablation_unit import verify_witness_scope
    row=scope_row(tmp_path);row['call_metrics']['entries'][0]['t']=4
    with pytest.raises(AssertionError):verify_witness_scope(row)


def test_scope_reader_rejects_validation_attached_to_another_proposal(tmp_path):
    from check_witness_ablation_unit import verify_witness_scope
    row=scope_row(tmp_path);row['call_metrics']['entries'][-1]['proposal_id']=9
    with pytest.raises(AssertionError):verify_witness_scope(row)
