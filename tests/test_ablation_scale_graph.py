import hashlib,json,math,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import score_leetcode_ablation as scoring

def graph():
    queries=[{'qid':c,'sql':'SELECT '+str(i)} for i,c in enumerate('abc')]
    row={'pairs':[{'a':'a','b':'b','certificate':0,'verdict':'split','accepted_s':2.0},
                  {'a':'a','b':'c','certificate':None,'verdict':'undecided'},
                  {'a':'b','b':'c','certificate':1,'verdict':'split','accepted_s':3.0}],
        'error_class':[],'budget':60,'dbs':[{},{}],'n_genuine_pairs':2,'last_accept_s':3.0}
    return queries,row

def test_exact_edge_union_keeps_unobserved_pair_unresolved():
    queries,row=graph();mask,counts,curve=scoring.pack_graph(queries,row)
    assert mask==0b101 and mask.bit_count()==2
    assert counts=={'split':2,'undecided':1} and curve==[0,1,2]
    # a-b and b-c witnesses do not infer anything about a-c.
    assert not mask&0b010

@pytest.mark.parametrize('mutation',('duplicate','late','error_certificate','backwards_database_time'))
def test_malformed_or_late_graph_is_rejected(mutation):
    queries,row=graph()
    if mutation=='duplicate':row['pairs'][1]=dict(row['pairs'][0])
    elif mutation=='late':row['pairs'][0]['accepted_s']=60
    elif mutation=='error_certificate':row['error_class']=['a']
    else:
        row['pairs'][0]['accepted_s']=4.0;row['last_accept_s']=4.0
    with pytest.raises(AssertionError):scoring.pack_graph(queries,row)

def test_database_prefix_includes_zero_database_batches_and_saturates():
    curves=[[0,2,3],[0]]
    assert [scoring.prefix_coverage(curves,k) for k in range(4)]==[0,2,3,3]

def test_database_chunks_have_distinct_receipt_times_but_count_one_database():
    queries,row=graph()
    row['pairs'][2]['certificate']=0
    row['dbs']=[{}]
    mask,counts,cumulative=scoring.pack_graph(queries,row)
    assert mask==0b101 and counts['split']==2 and cumulative==[0,2]

def test_sample_sd_uses_repeats_and_missing_metrics_stay_missing():
    result=scoring.mean_sd([1,2,3,4,5])
    assert result['mean']==3 and math.isclose(result['sample_sd'],math.sqrt(2.5))
    assert scoring.mean_sd([1,2,None,4,5])=={'mean':None,'sample_sd':None,'repeats_observed':4}
    assert scoring.mean_sd([None]*5)=={'mean':None,'sample_sd':None,'repeats_observed':0}

def test_late_native_return_stays_in_raw_but_not_in_budget_summary():
    entries=[{'kind':'generation_api_finished','verdict':'eq','within_acceptance_window':True},
             {'kind':'generation_api_finished','verdict':'error','within_acceptance_window':False}]
    assert scoring.native_returns(entries)=={'eq':1}
    assert scoring.native_returns(entries,in_window=False)=={'eq':1,'error':1}

def test_hash_binding_rejects_raw_or_checked_tampering(tmp_path):
    raw=tmp_path/'raw.jsonl';raw.write_text('{"pairs": []}\n')
    checked=tmp_path/'checked.json';checked.write_text(json.dumps({'raw_sha256':scoring.sha(raw)}))
    marker={'protocol_sha256':'p','raw_sha256':scoring.sha(raw),'checked_sha256':scoring.sha(checked)}
    (tmp_path/'done.json').write_text(json.dumps(marker))
    assert scoring.read_verified_unit(tmp_path,'p')[2]=={'pairs':[]}
    checked.write_text('{}')
    with pytest.raises(AssertionError):scoring.read_verified_unit(tmp_path,'p')
    checked.write_text(json.dumps({'raw_sha256':marker['raw_sha256']}))
    raw.write_text('{"pairs": [1]}\n')
    with pytest.raises(AssertionError):scoring.read_verified_unit(tmp_path,'p')

def test_incomplete_cohort_never_calls_aggregation_or_publishes(tmp_path,monkeypatch,capsys):
    import leetcode_ablation_protocol
    protocol={'version':'leetcode-ablation-scale-v1','test_ids':['test-only'],
              'methods':{'pair_seer':168},'seeds':[0],'expected_test_units':1}
    (tmp_path/'protocol.json').write_text(json.dumps(protocol))
    monkeypatch.setattr(leetcode_ablation_protocol,'check',lambda:protocol)
    monkeypatch.setattr(scoring,'OUT',tmp_path)
    monkeypatch.setattr(sys,'argv',['score_leetcode_ablation.py'])
    def forbidden(*args):raise AssertionError('incomplete scores attempted')
    monkeypatch.setattr(scoring,'aggregate',forbidden)
    class State:
        stdout='ActiveState=active\nMainPID=123\n'
    monkeypatch.setattr(scoring.subprocess,'run',lambda *args,**kwargs:State())
    assert scoring.main()==0
    output=json.loads(capsys.readouterr().out)
    assert output['state']=='waiting_no_scores_published' and output['missing_units']==1
    assert not (tmp_path/'scores').exists()
