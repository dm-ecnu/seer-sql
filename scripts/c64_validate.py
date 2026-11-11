"""Author-authorized class-method development gate on frozen input workloads."""
import argparse, json, os, sys, time
from pathlib import Path
from c63_consistency import R,P,NEW,PY,read,write,sha,selection
sys.path[:0]=[str(NEW/'src'),str(NEW/'eval')]


def load(job):
    from seer2.schema import Schema
    from seereval.schema import Schema as EvalSchema,to_ddl
    from seereval.engine import normalize_sql
    from seer_sqlwitness import sigma_extra,DIALECT,BACKEND
    cfg=read(job['config']);source=Path(job['input'])
    batch=next(b for b in map(json.loads,source.read_text().splitlines()) if b['batch_id']==job['batch'])
    es=EvalSchema.load((source.parent/batch['schema_path']).resolve());dialect=DIALECT[batch['dialect']]
    assert dialect in ('sqlite','postgres'),'MySQL is not authorized'
    conversion=batch.get('meta',{}).get('sqlite_conversion')
    ddl=es.native['sqlite_panel']['ddl'] if conversion else to_ddl(es,batch['dialect'],deferrable=False)
    schema=Schema(';\n'.join(ddl)+';',dialect,inclusions=sigma_extra(es))
    unsupported=set(conversion['unsupported_qids']) if conversion else set()
    queries=[(q['qid'],normalize_sql(q['sql'],batch['dialect'])) for q in batch['queries'] if q['qid'] not in unsupported]
    assert [q['qid'] for q in batch['queries']]==cfg['query_ids']
    return cfg,schema,es,dialect,BACKEND[dialect],queries


def verify_singleton_labels(schema,dialect,backend,queries,cfg,out):
    from seer.stream import StreamPartition
    from seer.stream_class import prove_classes
    from seer.stream_prover import NativeSQLSolver
    records=[]
    runtime=next(p for p in cfg['files'] if p.endswith('/runtime.json'))
    for i,(q,sql) in enumerate(queries[1:]):
        rep,reference=queries[0];labels={}
        for mode in ('individual','class'):
            native=NativeSQLSolver.from_json(runtime,schema=schema,dialect=dialect,
                backend=backend,evidence_dir=out/'singletons'/str(i)/mode/'native')
            e=StreamPartition(schema,dialect,backend,budget_s=cfg['budget_s'],
                per_call_s=cfg['per_call_s'],random_dbs=cfg['initial_random_dbs'],bounds=(1,2),prover=native)
            e.evidence_dir=str(out/'singletons'/str(i)/mode)
            try:
                e.add(rep,reference,compare=False);e.add(q,sql,compare=False)
                if mode=='class':prove_classes(e)
                else:e._compare(q,rep=rep,use_quick=False)
                e.assert_invariants(replay=True)
                if e.fingerprints[q]!=e.fingerprints[rep]:label={'status':'separated'}
                else:
                    relation=e.relation(q,rep)
                    label={key:relation.get(key) for key in ('status','k')}
                labels[mode]=label
            except BaseException as exc:
                e._save_replay_context('singleton_label_validation',exception=repr(exc))
                raise
            finally:
                native.close();e.search.close_worker()
        assert labels['class']==labels['individual'],(rep,q,labels)
        records.append(dict(representative=rep,query=q,**labels))
    return records


def validate(job,out):
    import subprocess
    from seer.stream_runner import run_stream
    from seer.class_validation import verify_bounded,verify_singletons
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    cfg,schema,es,dialect,backend,queries=load(job)
    sources={str(p):sha(p) for folder in ('src/seer','src/seer2') for p in (NEW/folder).glob('*.py')}
    write(out/'config.json',dict(job=job,source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=NEW,text=True).strip(),
        bounds=[1,2],development_only=True,files=sources,budget_s=cfg['budget_s'],per_call_s=cfg['per_call_s']))
    start=time.perf_counter()
    runtime=next(p for p in cfg['files'] if p.endswith('/runtime.json'))
    engine,result=run_stream(schema,queries,dialect,backend,cfg['budget_s'],0,runtime,out/'native',
        enable_prover=True,quick_sqlwitness=True,eval_schema=es,random_dbs=cfg['initial_random_dbs'],
        schedule='class_bounded',bounds=(1,2),per_call_s=cfg['per_call_s'])
    # Actual source representative/member pairs, plus all members in the
    # initial reference class; never synthesize duplicate/self pairs to pass.
    assert set(engine.arrival)=={q for q,_ in queries}, 'incomplete arrival cannot validate all singleton pairs'
    pairs=[(queries[0][0],q) for q,_ in queries[1:]]
    if not pairs:
        assert len(queries)<=1 and len(engine.edges)==0
        if queries:assert engine.relation(queries[0][0])=={'status':'representative'}
    assertions=dict(singleton_checks=(verify_singletons(engine.search,pairs,seconds=cfg['per_call_s'])
                     if pairs else [{'status':'no_candidate_after_frozen_conversion'}]),
                    **verify_bounded(engine,seconds=cfg['per_call_s']))
    assertions['singleton_labels']=verify_singleton_labels(schema,dialect,backend,queries,cfg,out)
    assert all(sha(p)==h for p,h in sources.items()),'validated source changed'
    result['class_assertions']=assertions
    result['development_only']=True
    result['assertions_passed']=True
    result['validation_total_wall_s']=time.perf_counter()-start
    write(out/'result.json',result)
    print(json.dumps({'passed':job['batch'],'singleton_checks':len(assertions['singleton_checks']),
                      'bounded_samples':len(assertions['bounded_samples'])}),flush=True)


if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--job',required=True);a.add_argument('--out',required=True)
    args=a.parse_args();validate(json.loads(args.job),args.out)
