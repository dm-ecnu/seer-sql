import copy,json,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from leetcode_ablation_protocol import VARIANTS,verify_configuration,sha
from hard_deadline import ABLATIONS
from score_leetcode_ablation import mechanism_metrics,prelude_comparisons


def fixture(tmp_path,variant='full',seed=0):
    config=ABLATIONS[variant]
    runtime={'python':'fixture','python_hash_seed':str(seed),'packages':{'z3-solver':'fixture'},'affinity':[184]}
    (tmp_path/'worker-runtime.json').write_text(json.dumps(runtime))
    state={'pending_pairs':[['a','b']],'accepted_databases':[]}
    (tmp_path/'pre-symbolic-state.json').write_text(json.dumps(state))
    event={'kind':'pre_symbolic_state','file':'pre-symbolic-state.json','sha256':sha(tmp_path/'pre-symbolic-state.json'),
           'pending_pairs':1,'accepted_databases':0,'elapsed_s':1}
    return {'tool':'pair_seer','ablation':variant,'encoding_configuration':config,
            'comparison_contract':{'ablation':variant,'encoding_configuration':config},
            'runtime_parent':runtime,'journal':str(tmp_path),'call_metrics':{'entries':[event]}}


def test_parent_and_spawned_worker_hash_seed_must_match(tmp_path):
    row=fixture(tmp_path);verify_configuration(row,'full',0)
    row['runtime_parent']=dict(row['runtime_parent'],python_hash_seed='random')
    with pytest.raises(AssertionError):verify_configuration(row,'full',0)


def test_wrong_switch_configuration_is_rejected(tmp_path):
    row=fixture(tmp_path)
    with pytest.raises(AssertionError):verify_configuration(row,'no_hard_sigma',0)


def test_pre_symbolic_state_is_bound_to_raw_file(tmp_path):
    row=fixture(tmp_path);(tmp_path/'pre-symbolic-state.json').write_text('{}')
    with pytest.raises(AssertionError):verify_configuration(row,'full',0)


def test_sigma_rejection_payload_is_bound(tmp_path):
    row=fixture(tmp_path,'no_hard_sigma')
    p=tmp_path/'sigma-rejection-000000.json';p.write_text(json.dumps({'violations':['NOT NULL']}))
    row['call_metrics']['entries'].append({'kind':'symbolic_sigma_rejected','database':p.name,'sha256':sha(p),'violations_count':1})
    verify_configuration(row,'no_hard_sigma',0)
    p.write_text('{}')
    with pytest.raises(AssertionError):verify_configuration(row,'no_hard_sigma',0)


def test_interrupted_encoding_is_lower_bound_and_not_completed_duration():
    events=[{'kind':'encoding_level_start','within_acceptance_window':True},
            {'kind':'encoding_level_built','within_acceptance_window':True,'encode_s':1.5,'encoding_stats':{'memo_miss':8}},
            {'kind':'encoding_level_start','within_acceptance_window':True}]
    metrics=mechanism_metrics({'call_metrics':{'entries':events}},[])
    assert metrics['encoding_levels_started']==2 and metrics['encoding_levels_completed']==1
    assert metrics['encoding_seconds_completed_lower_bound']==1.5


def test_unmatched_preludes_remain_in_full_population():
    rows=[{'batch_id':'b','seed':0,'n':10,'method':v,'pre_symbolic_sha256':str(i)} for i,v in enumerate(VARIANTS)]
    result=prelude_comparisons(rows,{'methods':VARIANTS})
    assert len(result)==1 and not result[0]['same_pre_symbolic_state']
    assert result[0]['all_variants_have_pre_symbolic_snapshot']
    for row in rows:row['pre_symbolic_sha256']=None
    result=prelude_comparisons(rows,{'methods':VARIANTS})
    assert not result[0]['all_variants_have_pre_symbolic_snapshot'] and not result[0]['same_pre_symbolic_state']


def test_runtime_version_drift_is_rejected(tmp_path):
    row=fixture(tmp_path)
    with pytest.raises(AssertionError):
        verify_configuration(row,'full',0,{'python':'fixture','packages':{'z3-solver':'changed'}})
