"""I/O adapter for SQLDriller's released checker, pinned at 6514019.

Uses check_equivalence unchanged, with the authors' sampled schema databases
and released/linked suites. Published SQLite panels are supported here; unseen
schemas require their corresponding native resources before evaluation.
The caller owns the common wall-clock cap and independent certificate validator.
Per-exec Docker CPU is returned separately from the legacy host-process meter.
Nonzero native exec exits mark Docker CPU accounting incomplete.
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import contextlib
import hashlib
import io
import os
from pathlib import Path
import random
import json
import sqlite3
import subprocess
import sys

from wproto import serve
from execution_deadline import deadline_command

ROOT=Path((SEER_ROOT))
UPSTREAM=ROOT/'sqldriller/upstream'
COMMIT='651401923af77db68f7f32b060f57da1b8e1e444'
CORE=('utils/utils.py','utils/sql_utils.py','third_party/ce_gen/evaluation.py',
      'third_party/ce_gen/check_equivance_by_verieql.py',
      'third_party/ce_gen/solver/counter_example.py','third_party/ce_gen/solver/prove_api.py')


def resources(schema):
    name=schema['name']
    # Only an exact schema name and column topology can select native data.
    for family,suite_suffix in (('bird','dbs/fuzz/bird/dev/original'),('spider','database')):
        base=ROOT/f'sqldriller/resources/native/{family}/schema_for_sample_values'
        db=base/name/(name+'.sqlite')
        suite=ROOT/'sqldriller/resources'/suite_suffix
        if not db.is_file() or not list((suite/name).glob('*.sqlite')):
            continue
        conn=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)
        try:
            native={r[0].lower():r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in schema['tables']:
                key=table['name'].lower()
                if key not in native:
                    break
                cols={r[1].lower() for r in conn.execute('PRAGMA table_info("'+native[key].replace('"','""')+'")')}
                if cols!={c['name'].lower() for c in table['columns']}:
                    break
            else:
                expected={t['name'].lower() for t in schema['tables']}
                if expected=={t for t in native if not t.startswith('sqlite_')}:
                    return family,base,suite
        finally:
            conn.close()
    return None


def handle(req):
    provenance={'upstream_commit':COMMIT,'implementation':'official',
                'value_generation_model':'local-qwen','docker_cpu_s':0,'docker_cpu_complete':True,'cpu_scope':'host_cpu_and_docker_cpu_separate'}
    if req['dialect'] not in ('sqlite','mysql'):
        return {**provenance,'verdict':'unsupported','reason':'native adapter requires SQLite input and matching published resources'}
    if req['dialect']=='mysql' and not req.get('resource_view'):
        return {**provenance,'verdict':'unsupported','reason':'MySQL requires explicit validated native SQLite proposal resources'}
    if req.get('require_declared_schema') and not req.get('resource_view'):
        return {**provenance,'verdict':'unsupported','reason':'declared-schema resources have not been prepared'}
    workspace=UPSTREAM
    if req.get('resource_view'):
        metadata=Path(req['resource_view']).resolve()
        assert metadata.is_relative_to(ROOT/'sqldriller/resources/declared'),metadata
        view=json.loads(metadata.read_text())
        assert view['upstream_commit']==COMMIT
        assert json.loads(Path(view['schema_path']).read_text())==req['schema'],'resource schema mismatch'
        from cubes_sqld_contract import offline_native_stages_allowed
        if not view['resources_ready'] and not (req['dialect']=='mysql' and offline_native_stages_allowed(view)):
            return {**provenance,'verdict':'unsupported','reason':'no published test database satisfies the declared schema','resource_key':view['resource_key']}
        from cubes_sqld_contract import native_database_id
        native_id=native_database_id(req,view)
        if req['dialect']=='mysql':
            provenance.update(value_generation_model=None,value_source='released_git_cache_only',model_fallback='blocked',
                              offline_stage_policy=view.get('offline_stage_policy'),
                              generator_cache_complete=bool(view.get('cache_coverage_verified')),
                              published_seed_ready=bool(view.get('test_suite_ready',view.get('resources_ready'))))
        found=(view['family'],Path(view['schema_db_base']),Path(view['suite']))
        provenance.update(native_db_id=native_id,native_execution_dialect='sqlite',target_dialect=req['dialect'],adapter_mode=view.get('adapter_mode','original_sqlite'),resource_view_sha256=hashlib.sha256(metadata.read_bytes()).hexdigest())
        provenance.update(resource_contract='declared_schema',resource_key=view['resource_key'],resource_preparation_wall_s=view.get('wall_s',view.get('offline_wall_seconds')),resource_preparation_cpu_s=view.get('cpu_s'))
    else:
        found=resources(req['schema'])
        provenance['resource_contract']='published_native'
    if found is None:
        return {**provenance,'verdict':'unsupported','reason':'matching native schema or test suites are missing'}
    actual=subprocess.check_output(['git','-C',str(UPSTREAM),'rev-parse','HEAD'],text=True).strip()
    assert actual==COMMIT,(actual,COMMIT)
    for file in CORE:
        expected=subprocess.check_output(['git','-C',str(UPSTREAM),'rev-parse',COMMIT+':'+file],text=True).strip()
        data=(UPSTREAM/file).read_bytes()
        blob=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        assert blob==expected,('core changed',file)
    # Each call begins with the released cache, never prior evaluation writes.
    # The host owns/removes this directory even when it kills the worker.
    workspace=Path(req["_call_workspace"])
    workspace.resolve().relative_to(ROOT/"repro/official/call_workspaces")
    assert workspace.is_dir()
    links=[("utils",UPSTREAM/"utils"),("third_party/solver",UPSTREAM/"third_party/solver")]
    if req.get("resource_view"):
        links.append(("data",metadata.parent/"data"))
    else:
        links.append(("data",UPSTREAM/"data"))
    for rel,target in links:
        link=workspace/rel
        link.parent.mkdir(parents=True,exist_ok=True)
        link.symlink_to(target,target_is_directory=True)
    cache_rel="third_party/ce_gen/solver/column_values.sqlite"
    cache_bytes=subprocess.check_output(["git","-C",str(UPSTREAM),"show",COMMIT+":"+cache_rel])
    cache=workspace/cache_rel
    cache.parent.mkdir(parents=True,exist_ok=True)
    cache.write_bytes(cache_bytes)
    provenance.update(value_cache_source="released_git_blob",value_cache_initial_sha256=hashlib.sha256(cache_bytes).hexdigest(),value_cache_isolation="per_call")
    os.chdir(workspace)
    sys.path.insert(0,str(UPSTREAM))
    os.environ['OPENAI_API_KEY']=os.environ['LLM_API_KEY']
    os.environ['OPENAI_API_BASE']='http://127.0.0.1:4000/v1'
    os.environ['NLTK_DATA']=str(ROOT/'sqldriller/resources/nltk_data')
    os.environ['PATH']=str(ROOT/'sqldriller/jdk17/bin')+':'+os.environ['PATH']
    random.seed(req.get('seed',0))
    import utils.llm_utils as settings
    settings.GPT_3_5_TURBO='local-qwen'
    from utils.constants import sql_equiv_mode
    from utils.utils import check_equivalence
    import utils.utils as native_utils
    original_solver=native_utils.check_equivalence_by_sqlsolver
    solver_answers=[]
    def observed_solver(*args,**kwargs):
        answer=original_solver(*args,**kwargs)
        solver_answers.append(str(answer))
        return answer
    family,base,suite=found
    native_id=provenance.get('native_db_id',req['schema']['name'])
    blocked_model_calls=[]
    native_ce=None
    original_native_reply=None
    if req['dialect']=='mysql':
        assert hashlib.sha256(cache_bytes).hexdigest()==view['cache_coverage']['cache_sha256']
        import third_party.ce_gen.solver.counter_example as native_ce
        original_native_reply=native_ce.gpt_reply
        def reject_uncached_model_call(*args,**kwargs):
            blocked_model_calls.append('released_cache_miss')
            raise RuntimeError('offline Cubes bridge forbids uncached model calls')
        native_ce.gpt_reply=reject_uncached_model_call
    from docker.models.containers import Container
    original_exec=Container.exec_run
    executions=[]
    def metered_exec(container,cmd,*args,**kwargs):
        if container.name!='seer-icde-sqldriller-verieql' or not isinstance(cmd,list) or cmd[:1]!=['timeout']:
            return original_exec(container,cmd,*args,**kwargs)
        # Docker execs are outside the host worker process group.
        bounded=deadline_command(cmd,req.get("_deadline_monotonic"))
        response=original_exec(container,['python','/tmp/seer_icde_exec_meter.py']+bounded,*args,**kwargs)
        marker=b'\n__ICDE_EXEC_CPU__'
        if marker not in response.output:
            executions.append({'cpu_complete':False,'missing_meter':True})
            return response
        output,metadata=response.output.rsplit(marker,1)
        metric=json.loads(metadata.strip())
        assert metric['exit_code']==response.exit_code,(metric,response.exit_code)
        executions.append(metric)
        return type(response)(response.exit_code,output)
    log=io.StringIO()
    Container.exec_run=metered_exec
    native_utils.check_equivalence_by_sqlsolver=observed_solver
    try:
        with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
            tag,certificate=check_equivalence(req['q1'],req['q2'],req['ddl'],str(base),str(suite),native_id,sql_equiv_mode.mixed,family)
    finally:
        Container.exec_run=original_exec
        native_utils.check_equivalence_by_sqlsolver=original_solver
        if native_ce is not None:
            native_ce.gpt_reply=original_native_reply
    native_log=log.getvalue().replace(os.environ['OPENAI_API_KEY'],'[REDACTED]')
    provenance['blocked_model_calls']=len(blocked_model_calls)
    print(native_log,file=sys.stderr,end='')
    from cubes_sqld_contract import native_stage_outcome
    out={**provenance,**native_stage_outcome(tag,len(blocked_model_calls)),
         'native_tag':tag,'native_family':family,'core_files_verified':len(CORE)}
    out['generator_model_fallback_blocked']=bool(blocked_model_calls)
    out["value_cache_final_sha256"]=hashlib.sha256(cache.read_bytes()).hexdigest()
    out['stage']='sqlsolver_eq' if tag==1 and solver_answers==['EQ'] else 'official_checker'
    out['sqlsolver_answers']=solver_answers
    out['docker_cpu_s']=sum(x.get('child_cpu_s',0)+x.get('meter_cpu_s',0) for x in executions)
    out['docker_execs']=executions
    out['docker_cpu_complete']=all(x.get('cpu_complete',False) for x in executions)
    out['native_exception_logged']=any(x in native_log for x in ('Traceback (most recent call last)','Exception in ','VeriEQL exception:','GPT exception:'))
    if tag==0:
        assert certificate and Path(certificate).is_file(),'NEQ without native certificate'
        con=sqlite3.connect(Path(certificate).as_uri()+'?mode=ro',uri=True)
        con.row_factory=sqlite3.Row
        try:
            instance={}
            for t in req['schema']['tables']:
                rows=con.execute('SELECT * FROM "'+t['name'].replace('"','""')+'"')
                values=[]
                for row in rows:
                    keys={k.lower():k for k in row.keys()}
                    values.append({c['name']:row[keys[c['name'].lower()]] for c in t['columns']})
                instance[t['name']]=values
            out['instance']=instance
            out['native_certificate']=str(certificate)
        finally:
            con.close()
    return out


if __name__=='__main__':
    serve(handle)
