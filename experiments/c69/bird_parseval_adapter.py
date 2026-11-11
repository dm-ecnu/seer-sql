"""Run the pinned official ParSEval BIRD pair driver in the common batch shell.

The initial seed-pool pair list follows official_batch.py. Subsequent native
pairs are not pruned using earlier pair outputs. No ParSEval code is changed.
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import itertools,json,os,random,tempfile,time
from pathlib import Path
from seer.stream import StreamPartition
from seer.stream_runner import PersistentPairTool
from seereval.schema import to_ddl
from seereval.engine import _coerce

R=Path((SEER_ROOT))
HERE=Path(__file__).resolve().parent

class OfficialSession(PersistentPairTool):
    def __init__(self,out):
        self.python=str(R/'parseval25/venv007/bin/python')
        self.worker=str(HERE/'parseval007_pool_worker.py')
        self.scratch=tempfile.TemporaryDirectory(prefix='c69-parseval007-',dir=os.environ['TMPDIR'])
        self.log=(out/'parseval007-stderr.log').open('ab')
        self.process=None;self.starts=0

def run(schema,eval_schema,queries,dialect,backend,budget_s,seed,order='source',
        start_time=None,method_seed=0,native_session_dir=None,**unused):
    assert dialect=='sqlite' and method_seed==0
    out=Path(native_session_dir).parent
    sqls=list(queries)
    if order=='shuffle':random.Random(seed).shuffle(sqls)
    elif order=='reverse':sqls.reverse()
    else:assert order=='source'
    engine=StreamPartition(schema,dialect,backend,budget_s=budget_s,seed=method_seed,start_time=start_time)
    for q,sql in sqls:
        if engine.remaining()<=0 or not engine.add(q,sql,compare=False):break
    targets=[(a,b) for a,b in itertools.combinations(engine.arrival,2)
             if engine.fingerprints[a]==engine.fingerprints[b]]
    ddl=';'.join(to_ddl(eval_schema,'sqlite',deferrable=False))
    calls=[];session=OfficialSession(out)
    try:
        for i,(a,b) in enumerate(targets):
            if engine.remaining()<=0:break
            began=time.perf_counter()
            record=session.call(dict(index=i,batch=out.name,ddl=ddl,q1=engine.queries[a],q2=engine.queries[b]),
                                min(360.,engine.remaining()))
            engine.metrics['refutation_search_s']+=time.perf_counter()-began
            call=dict(a=a,b=b,native=record,wall_s=time.perf_counter()-engine.started)
            calls.append(call)
            with (out/'parseval007-completed.jsonl').open('a') as h:h.write(json.dumps(call,default=str)+'\n')
            if record.get('verdict')=='neq' and record.get('instance') is not None:
                original=record['instance'];db={}
                for table in eval_schema.tables:
                    rows=next((v for k,v in original.items() if k.lower()==table.name.lower()),[])
                    db[table.name.lower()]=[]
                    for row in rows:
                        low={str(k).lower():v for k,v in row.items()}
                        db[table.name.lower()].append({c['name'].lower():_coerce(low.get(c['name'].lower()),c['type']) for c in table.columns})
                call['engine_admitted']=engine._install(db,target=(a,b),admission_deadline=engine.deadline)
    finally:session.close()
    partition_wall=time.perf_counter()-engine.started
    engine.assert_invariants(replay=True)
    result=engine.snapshot()
    result.update(tool='parseval',shell='all',calls=calls,
        native_mode='official_release007_bird_pair_function_defaults',
        official_driver_requested_pairs=len(targets),
        native_solver_coverage='Official v0.0.7 pair API requests; internal native SMT calls are opaque',
        unprocessed_query_ids=[q for q,_ in sqls[len(engine.arrival):]])
    result['metrics'].update(input_queries=len(sqls),processed_queries=len(engine.arrival),
        unprocessed_queries=len(sqls)-len(engine.arrival),native_pair_calls=len(calls),
        pair_decision_requests=len(calls),solver_calls=0,partition_wall_s=partition_wall,
        native_worker_starts=session.starts,
        native_errors=sum(c['native'].get('verdict') in ('error','syntax_error') for c in calls),
        native_timeouts=sum(c['native'].get('verdict')=='timeout' for c in calls))
    return engine,result
