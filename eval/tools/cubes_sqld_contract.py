"""Fail-closed input contract for official SQLite proposals on MySQL Cubes."""
import re
MODE='official_sqlite_proposals_fresh_mysql_certificate_acceptance'
OFFLINE_STAGE_POLICY='released_cache_only_block_models_preserve_native_stages_v1'
RELEASED_CACHE_SHA='f21306cca1d71fa7d3c1f312f8d525151e9f49f6dcb39ba84da13ce1ea5ccf18'

def offline_native_stages_allowed(view):
    return (view.get('offline_stage_policy')==OFFLINE_STAGE_POLICY
            and view.get('interface_ready') is True
            and view.get('adapter_mode')==MODE
            and view.get('native_execution_dialect')=='sqlite'
            and view.get('target_dialect')=='mysql'
            and view.get('cache_coverage',{}).get('cache_sha256')==RELEASED_CACHE_SHA)

def native_stage_outcome(tag,blocked_model_calls):
    verdict={1:'eq',0:'neq',-1:'unsupported'}[tag]
    if tag==-1 and blocked_model_calls:
        return {'verdict':'error','reason':'released_cache_missing_generator_unavailable_native_stages_exhausted'}
    return {'verdict':verdict}

def native_database_id(req,view):
    if req['dialect']=='sqlite':return view.get('native_db_id',req['schema']['name'])
    if req['dialect']!='mysql':raise ValueError('unvalidated target dialect')
    if view.get('adapter_mode')!=MODE or view.get('native_execution_dialect')!='sqlite' or view.get('target_dialect')!='mysql':raise ValueError('explicit native/target contract missing')
    partial=offline_native_stages_allowed(view)
    if (not view.get('resources_ready') or not view.get('cache_coverage_verified')) and not partial:raise ValueError('native resources/cache not verified')
    coverage=view.get('cache_coverage',{})
    if not partial and (coverage.get('missing') or coverage.get('invalid_or_empty_payload') or not coverage.get('cache_sha256')):raise ValueError('released cache is incomplete')
    native=view.get('native_db_id','')
    if not re.fullmatch('[A-Za-z0-9_]+',native):raise ValueError('invalid canonical database identity')
    return native
