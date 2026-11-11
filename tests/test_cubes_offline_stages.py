import copy,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'eval/tools'))
from cubes_sqld_contract import MODE,OFFLINE_STAGE_POLICY,RELEASED_CACHE_SHA,offline_native_stages_allowed,native_database_id,native_stage_outcome

def inputs():
 return {'dialect':'mysql','schema':{'name':'original_name'}},{'adapter_mode':MODE,'native_execution_dialect':'sqlite','target_dialect':'mysql','resources_ready':False,'interface_ready':True,'test_suite_ready':True,'cache_coverage_verified':False,'cache_coverage':{'cache_sha256':RELEASED_CACHE_SHA,'missing':[['db','T','C']]},'offline_stage_policy':OFFLINE_STAGE_POLICY,'native_db_id':'db'}

def test_explicit_partial_cache_can_reach_unchanged_native_stages():
 req,view=inputs();before=copy.deepcopy((req,view))
 assert offline_native_stages_allowed(view) and native_database_id(req,view)=='db'
 assert (req,view)==before and not view['resources_ready'] and not view['cache_coverage_verified']

@pytest.mark.parametrize('key,value',[('offline_stage_policy',None),('interface_ready',False),('adapter_mode',''),('target_dialect','sqlite'),('native_execution_dialect','mysql')])
def test_partial_permission_requires_exact_policy_and_interfaces(key,value):
 req,v=inputs();v[key]=value
 with pytest.raises(ValueError):native_database_id(req,v)

def test_working_copy_cache_not_accepted():
 req,v=inputs();v['cache_coverage']['cache_sha256']='working-copy'
 with pytest.raises(ValueError):native_database_id(req,v)

def test_seed_absence_is_not_generator_or_solver_unsupported():
 req,v=inputs();v['test_suite_ready']=False
 assert native_database_id(req,v)=='db'

def test_valid_native_neq_survives_blocked_model_attempt():
 assert native_stage_outcome(0,3)=={'verdict':'neq'}

def test_native_eq_is_retained_as_native_result():
 assert native_stage_outcome(1,3)=={'verdict':'eq'}

def test_resource_exhaustion_is_not_silent_quality_zero():
 r=native_stage_outcome(-1,3);assert r['verdict']=='error' and 'generator_unavailable' in r['reason']
 assert native_stage_outcome(-1,0)=={'verdict':'unsupported'}
