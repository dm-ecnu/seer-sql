import copy,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'eval/tools'))
from cubes_sqld_contract import MODE,native_database_id

def inputs():
 return {'dialect':'mysql','schema':{'name':'spider_candidate_poll_tables_db'},'q1':'SELECT * FROM candidate','q2':'SELECT * FROM candidate LIMIT 1'},{'adapter_mode':MODE,'native_execution_dialect':'sqlite','target_dialect':'mysql','resources_ready':True,'cache_coverage_verified':True,'cache_coverage':{'missing':[],'invalid_or_empty_payload':[],'cache_sha256':'released-cache-hash'},'native_db_id':'candidate_poll'}
def test_canonical_resource_id_preserves_target_schema_and_raw_queries():
 req,view=inputs();before=copy.deepcopy(req)
 assert native_database_id(req,view)=='candidate_poll' and req==before
@pytest.mark.parametrize('key,value',[('target_dialect','sqlite'),('native_execution_dialect','mysql'),('adapter_mode',''),('resources_ready',False),('cache_coverage_verified',False),('native_db_id','../candidate_poll')])
def test_unverified_or_mismatched_bridge_rejected(key,value):
 req,view=inputs();view[key]=value
 with pytest.raises(ValueError):native_database_id(req,view)
def test_unseen_target_and_cold_cache_rejected():
 req,view=inputs();req['dialect']='postgresql'
 with pytest.raises(ValueError):native_database_id(req,view)
 req,view=inputs();view['cache_coverage']['missing']=[['db','T','C']]
 with pytest.raises(ValueError):native_database_id(req,view)
def test_original_sqlite_identity_retained():
 assert native_database_id({'dialect':'sqlite','schema':{'name':'original'}},{})=='original'

@pytest.mark.parametrize('dialect,reason',[('mysql','MySQL requires explicit'),('postgresql','requires SQLite'),('sqlite','declared-schema resources')])
def test_actual_worker_rejects_unprepared_requests_before_algorithms(dialect,reason):
 from sqldriller_official_worker import handle
 result=handle({'dialect':dialect,'schema':{'name':'unprepared'},'require_declared_schema':True})
 assert result['verdict']=='unsupported' and reason in result['reason']
