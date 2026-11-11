"""Optional append-only observations of existing Python API entry attempts.

No SQL text, credentials or argument values are recorded. Official package files
are unchanged; wrappers preserve results and exceptions. Opaque Java internals
are explicitly unobserved.
"""
import functools
import contextlib
import contextvars
import hashlib
import itertools
import json
import os
from pathlib import Path
import time

_fd=None
_role=None
_call_id=None
_seq=itertools.count()

def configure(path,role,call_id=None):
    global _fd,_role,_call_id
    if _fd is None:_fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_CLOEXEC,0o600)
    _role,_call_id=role,call_id

def record(kind,**fields):
    if _fd is None:return
    line=json.dumps({'kind':kind,'pid':os.getpid(),'t':time.monotonic(),'role':_role,'call_id':_call_id,**fields},sort_keys=True)+'\n'
    data=line.encode();assert len(data)<=4096
    assert os.write(_fd,data)==len(data),'incomplete metric entry'

_validation_context=contextvars.ContextVar('seer_common_validation',default=None)

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()

def request_identity(tool,request):
    # Explicit allowlist: URL, credentials, arbitrary request keys and SQL
    # plaintext never enter the retained identity.
    query_fields={}
    for key in ('q1','q2','queries','mutants'):
        if key not in request:continue
        values=request[key] if isinstance(request[key],list) else [request[key]]
        query_fields[key]=[hashlib.sha256(q.encode()).hexdigest() for q in values]
    schemas={key:fingerprint(request[key]) for key in
             ('schema','ddl','tables','types','constraints','omitted_constraints') if key in request}
    return {'tool':tool,'query_sha256':query_fields,'schema_component_sha256':schemas,
            'seed':request.get('seed'),'timeout':request.get('timeout'),
            'dialect':request.get('dialect'),'bound':request.get('bound'),
            'definition':'exact request SQL bytes hashed; not a normalized query or equivalence label'}

def bind_request(tool,request,journal,call_id):
    path=Path(journal)/('native-request-'+call_id+'.json')
    data=json.dumps(request_identity(tool,request),sort_keys=True).encode()
    with path.open('xb') as stream:stream.write(data)
    return {'request_file':path.name,'request_sha256':hashlib.sha256(data).hexdigest()}

def install_validation_observer():
    from seereval import validator
    original=validator.check_db
    if getattr(original,'_seer_validation_observer',False):return
    @functools.wraps(original)
    def checked(*args,**kwargs):
        context=_validation_context.get()
        if context is None:return original(*args,**kwargs)
        context['checks']+=1
        fields={'offer_id':context['offer_id'],'check_index':context['checks']}
        record('common_validation_attempt',**fields)
        try:result=original(*args,**kwargs)
        except BaseException as exc:
            record('common_validation_exception',exception_type=type(exc).__name__,**fields)
            raise
        record('common_validation_finished',status=result['status'],
               violations_count=len(result['violations']),
               load_error_sha256=fingerprint(result['load_error']) if result.get('load_error') else None,
               **fields)
        return result
    checked._seer_validation_observer=True
    validator.check_db=checked

@contextlib.contextmanager
def observe_common_offer(offer_id,instance):
    context={'offer_id':offer_id,'checks':0}
    token=_validation_context.set(context)
    try:
        record('common_offer_attempt',offer_id=offer_id,instance_sha256=fingerprint(instance),
               fingerprint_encoding='json_sort_keys_default_str',
               scope='common admission only; internally filtered native proposals unobserved')
        try:yield
        except BaseException as exc:
            record('common_offer_exception',offer_id=offer_id,exception_type=type(exc).__name__,checks=context['checks'])
            raise
        else:record('common_offer_returned',offer_id=offer_id,checks=context['checks'])
    finally:_validation_context.reset(token)

def wrap(owner,name,kind):
    original=getattr(owner,name)
    if getattr(original,'_seer_entry_metric',False):return
    @functools.wraps(original)
    def observed(*args,**kwargs):
        record(kind)
        return original(*args,**kwargs)
    observed._seer_entry_metric=True
    setattr(owner,name,observed)

def install_solver():
    import z3
    for cls in (z3.Solver,z3.Optimize):wrap(cls,'check','python_solver_check_attempt')

def install_host(path,method):
    configure(path,'host:'+method)
    install_validation_observer()
    from seereval import engine
    for name in ('SQLiteEngine','PostgresEngine','MySQLEngine','DuckDBEngine'):
        cls=getattr(engine,name)
        wrap(cls,'run','common_query_execute_attempt')
        wrap(cls,'load','common_database_load_attempt')
    if method=='pair_seer':
        install_solver()
        from seer2 import backends
        for name in ('_DuckInst','_SqliteInst','_PgInst','_MyInst'):
            wrap(getattr(backends,name),'run','source_query_execute_attempt')
        for name in ('DuckDBBackend','SQLiteBackend','PostgresBackend','MySQLBackend'):
            wrap(getattr(backends,name),'open','source_database_load_attempt')
    record('coverage',component='host',python_solver=method=='pair_seer',source_queries=method=='pair_seer',common_queries=True)

def install_native(req):
    path=os.environ.get('SEER_ENTRY_METRICS')
    if not path:return
    method=req['_entry_metric_tool'];call_id=req['_entry_metric_call_id']
    configure(path,'native:'+method,call_id)
    cgroup=next(l for l in Path('/proc/self/cgroup').read_text().splitlines() if l.startswith('0::')).split('::',1)[1]
    record('native_worker_started',tool=method,cgroup=cgroup)
    if method=='sqlwitness':
        from sqlwitness.online.runner import DatabaseRunner
        wrap(DatabaseRunner,'execute_query','source_query_execute_attempt')
        record('coverage',component='native',python_solver='not_used',source_queries=True)
    elif method=='parseval':
        install_solver()
        import importlib
        native=importlib.import_module('parseval.main')
        wrap(native,'_run','source_query_execute_attempt')
        record('coverage',component='native',python_solver=True,source_queries=True)
    elif method=='xdatan':
        record('coverage',component='native',python_solver=False,source_queries=False,opaque_java=True)
    elif method=='sqldriller_official':
        from official_lifecycle import install_native as install_official
        install_official()
    else:raise ValueError('unverified native metric observer: '+method)

def instrument_generation(deadline,journal):
    from seereval import tools
    original=tools.call
    def observed(tool,req,hard_s,mem_gb=None):
        call_id=f'{os.getpid()}-{next(_seq)}'
        record('generation_api_start',tool=tool,attempt_id=call_id,n_queries=1+len(req.get('mutants',[])) if tool=='xdatan' else 2)
        remaining=deadline-time.monotonic()
        if remaining<=0:return {'verdict':'timeout','reason':'parent_deadline'}
        request=dict(req,_entry_metric_tool=tool,_entry_metric_call_id=call_id)
        request['timeout']=min(request['timeout'],remaining)
        identity=bind_request(tool,request,journal,call_id)
        record('generation_request_bound',tool=tool,attempt_id=call_id,**identity)
        try:
            if tool=='sqldriller_official':
                from official_lifecycle import observe_workspaces
                with observe_workspaces(call_id):
                    result=original(tool,request,min(hard_s,remaining),mem_gb=mem_gb)
            else:
                result=original(tool,request,min(hard_s,remaining),mem_gb=mem_gb)
        except BaseException as exc:
            record('generation_api_exception',tool=tool,attempt_id=call_id,exception_type=type(exc).__name__,**identity)
            raise
        path=Path(journal)/('native-result-'+call_id+'.json')
        data=json.dumps(result,sort_keys=True,default=str).encode()
        with path.open('xb') as stream:stream.write(data)
        record('generation_api_finished',tool=tool,attempt_id=call_id,verdict=result.get('verdict'),result_file=path.name,result_sha256=hashlib.sha256(data).hexdigest(),**identity)
        return result
    tools.call=observed

def read_entries(path,start,deadline):
    path=Path(path)
    if not path.exists():return {'counts':{},'entries':[],'partial_line':False,'opaque_native_internals':False,'definition':'API entry attempts, not completed work'}
    data=path.read_bytes();lines=data.splitlines(keepends=True);entries=[];partial=False
    for i,line in enumerate(lines):
        if not line.endswith(b'\n'):
            assert i==len(lines)-1;partial=True;break
        row=json.loads(line);assert isinstance(row['t'],(int,float))
        row['started_s']=row['t']-start;row['within_acceptance_window']=start<=row['t']<deadline
        entries.append(row)
    from collections import Counter
    counted=Counter(r['kind'] for r in entries if r['within_acceptance_window'] and (r['kind'].endswith('_attempt') or r['kind']=='generation_api_start'))
    return {'counts':dict(counted),'entries':entries,'partial_line':partial,'opaque_native_internals':any(r.get('opaque_java') for r in entries),'definition':'API entry attempts; starts logged before invoking the original function; no success or Java-internal inference'}

def verify_generation_observations(journal,entries):
    """Bind declared request/result files; interrupted calls remain incomplete."""
    journal=Path(journal);calls={}
    for entry in entries:
        if entry['kind'] not in ('generation_api_start','generation_request_bound',
                                 'generation_api_finished','generation_api_exception'):continue
        cid=entry['attempt_id'];states=calls.setdefault(cid,{})
        assert entry['kind'] not in states,'duplicate generation observation'
        states[entry['kind']]=entry
    rows=[]
    for cid,states in calls.items():
        assert 'generation_api_start' in states
        start=states['generation_api_start'];bound=states.get('generation_request_bound')
        finish=states.get('generation_api_finished');error=states.get('generation_api_exception')
        assert not (finish and error)
        if bound:
            name=bound['request_file'];assert Path(name).name==name
            path=journal/name;data=path.read_bytes()
            assert hashlib.sha256(data).hexdigest()==bound['request_sha256'],'request identity changed'
            identity=json.loads(data);assert identity['tool']==start['tool']
        if finish or error:
            end=finish or error;assert bound,'missing request observation'
            assert all(end[k]==bound[k] for k in ('request_file','request_sha256'))
        if finish:
            name=finish['result_file'];assert Path(name).name==name
            data=(journal/name).read_bytes()
            assert hashlib.sha256(data).hexdigest()==finish['result_sha256'],'native result changed'
            assert json.loads(data).get('verdict')==finish['verdict']
        rows.append({'attempt_id':cid,'tool':start['tool'],
                     'state':'completed_return' if finish else 'exception' if error else 'unfinished',
                     'request_observed':bool(bound),
                     'finished_in_window':finish.get('within_acceptance_window') if finish else None})
    return rows
