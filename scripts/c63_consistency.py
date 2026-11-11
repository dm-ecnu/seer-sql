"""Fail-closed repair gate on pre-frozen workloads; never launches formal reruns."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse, hashlib, itertools, json, os, subprocess, sys, time
import concurrent.futures as cf
from pathlib import Path
R=Path((SEER_ROOT))
P=R/'repro'
OLD=R/'seer-m33-sqlite-main-wt'
NEW=Path(__file__).resolve().parents[1]
PY=R/'venv-seer/bin/python'
NAMES={'spider':'spider_sqlwitness_original_batches.jsonl',
       'bird_sw':'bird_sqlwitness_original_batches.jsonl', 'xdatabm':'xdatabm.jsonl',
       'calcite':'calcite_verieql_paper_pairs.jsonl',
       'literature':'literature_verieql_paper_pairs.jsonl',
       'leetcode':'leetcode_verieql_original_batches.jsonl'}
def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,value):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
 tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2,default=str)+'\n');tmp.replace(p)
def worker(tree, job, out):
 tree=Path(tree);out=Path(out);out.mkdir(parents=True,exist_ok=False)
 sys.path[:0]=[str(tree/'src'),str(tree/'eval'),str(tree/'scripts')]
 from seer.core import _jsonable
 from seer.stream_runner import run_stream
 from seer2.schema import Schema
 from seereval.schema import Schema as EvalSchema, to_ddl
 from seereval.engine import normalize_sql
 from seer_sqlwitness import sigma_extra,DIALECT,BACKEND
 import run_stream_batch as runner
 cfg=read(job['config']);source=Path(job['input']);started=time.perf_counter()
 batch=next(x for x in map(json.loads,source.read_text().splitlines()) if x['batch_id']==job['batch'])
 schema_path=(source.parent/batch['schema_path']).resolve()
 es=EvalSchema.load(schema_path);dialect=DIALECT[batch['dialect']]
 conversion=batch.get('meta',{}).get('sqlite_conversion')
 ddl=es.native['sqlite_panel']['ddl'] if conversion else to_ddl(es,batch['dialect'],deferrable=False)
 schema=Schema(';\n'.join(ddl)+';',dialect,inclusions=sigma_extra(es))
 unsupported=set(conversion['unsupported_qids']) if conversion else set()
 queries=[(q['qid'],normalize_sql(q['sql'],batch['dialect'])) for q in batch['queries'] if q['qid'] not in unsupported]
 assert [q['qid'] for q in batch['queries']]==cfg['query_ids']
 native=next(Path(p) for p in cfg['files'] if p.endswith('/runtime.json'))
 sources={str(p):sha(p) for path in ('src/seer','src/seer2') for p in (tree/path).glob('*.py')}
 write(out/'config.json',dict(job=job,source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=tree,text=True).strip(),
       budget_s=cfg['budget_s'],per_call_s=cfg['per_call_s'],seed=cfg['seed'],affinity=sorted(os.sched_getaffinity(0)),
       input_sha256=sha(source),schema_sha256=sha(schema_path),files=sources))
 os.environ['SEER_REPLAY_EVIDENCE_DIR']=str(out)
 runner.setup_meter(out,cfg['method'])
 engine,result=run_stream(schema,queries,dialect,BACKEND[dialect],cfg['budget_s'],cfg['seed'],native,out/'native',
   order=cfg['order'],random_dbs=cfg['initial_random_dbs'],eval_schema=es,
   quick_sqlwitness=cfg['method']=='seer_stream_sqlwitness',start_time=started,
   method_seed=cfg.get('method_seed',0),schedule=cfg['stream_schedule'],
   candidate_budget_s=cfg['candidate_budget_s'],quick_failure_cap=cfg['quick_failure_cap'],
   separation_only=cfg['separation_only'],per_call_s=cfg['per_call_s'],quick_schedule=cfg['quick_schedule'],
   remainder_quick_comparisons=cfg['remainder_quick_comparisons'],
   no_certificate_transfer=cfg.get('no_certificate_transfer',False),single_bucket_target=cfg.get('single_bucket_target',False))
 result['witnesses']=[dict(database=_jsonable(w.database),domain=w.domain,k=w.k,active=w.active,mode=w.mode,reason=w.reason)
                       for w in engine.witnesses]
 result['original_unsupported_query_ids']=sorted(unsupported)
 assert all(sha(p)==h for p,h in sources.items()),'source changed during gate run'
 assert result['metrics']['invalid_admitted_certificates']==0
 write(out/'result.json',result)

def selection():
 sample=read(P/'dstM3-pair-v1/sample.json');groups={}
 for panel in NAMES:
  source=(P/'dstM3-sqlite-m36/inputs' if panel in ('calcite','literature','leetcode') else R/'datasets/batches')/NAMES[panel]
  batches={b['batch_id']:b for b in map(json.loads,source.read_text().splitlines())}
  if panel in ('spider','bird_sw'):base=P/'dstM3-pair-v1'/panel;ids=sample[panel]['batches']
  elif panel=='xdatabm':base=P/'dstM3-pair-xdata-v4'/panel;ids=sample[panel]['batches']
  else:base=P/('dstM3-sqlite-m36' if panel=='leetcode' else 'dstM3-sqlite-m31')/'main'/panel;ids=list(batches)
  candidates=[]
  source_sha=sha(source)
  ids=sorted(ids,key=lambda b:hashlib.sha256(('codex63-v1:'+b).encode()).hexdigest())
  count=10 if panel=='leetcode' else 20
  for b in ids:
   if not batches[b].get('schema_path'):continue
   c=base/b/'seer-s0/config.json';result=base/b/'seer-s0/result.json'
   if not c.exists() or not result.exists():continue
   cfg=read(c)
   if 'initial_random_dbs' not in cfg:continue
   if panel=='leetcode':
    m=read(result)['metrics']
    if m.get('censored_at_budget') or not m.get('completed_within_budget'):continue
   candidates.append(dict(panel=panel,batch=b,seed=0,input=str(source),config=str(c),
                          input_sha256=source_sha,old_config_sha256=sha(c)))
   if len(candidates)==count:break
  print(json.dumps({'event':'selected','panel':panel,'count':len(candidates)}),flush=True)
  # Selection is solely hash order within the pre-frozen roster; no selection
  # on output equivalence, solver depth, query count or speed.
  candidates.sort(key=lambda j:hashlib.sha256(('codex63-v1:'+j['batch']).encode()).hexdigest())
  count=10 if panel=='leetcode' else 20
  assert len(candidates)>=count,(panel,len(candidates),count)
  groups[panel]=candidates[:count]
 return [job for panel in NAMES for job in groups[panel]]

# Strip measurement/provenance fields only. Keep bound, domain, safety guards,
# query/schema digests, actual witness values, labels, reasons and membership.
META={'wall_s','solve_s','encoding_reused','encoding_sources','solver_calls','native_wall_s','elapsed_s'}
def clean(value):
 if isinstance(value,dict):return {k:clean(v) for k,v in value.items() if k not in META}
 if isinstance(value,list):return [clean(v) for v in value]
 return value

def output_signature(result):
 fields=('contract','classes','representatives','relations','pair_counts','domain_identity',
         'evidence_edges','witnesses','original_unsupported_query_ids')
 return {k:clean(result.get(k)) for k in fields}
def differences(a,b,path=''):
 if type(a)!=type(b):return [dict(path=path,old=a,new=b)]
 if isinstance(a,dict):
  out=[]
  for k in sorted(a.keys()|b.keys()):out+=differences(a.get(k),b.get(k),path+'/'+str(k))
  return out
 if isinstance(a,list):
  if len(a)!=len(b):return [dict(path=path+'/length',old=len(a),new=len(b))]
  out=[]
  for i,(x,y) in enumerate(zip(a,b)):out+=differences(x,y,path+'/'+str(i))
  return out
 return [] if a==b else [dict(path=path,old=a,new=b)]

def pair(job,out):
 out=Path(out);c=read(job['config']);budget=c['budget_s']
 for name,tree in (('old',OLD),('new',NEW)):
  env=dict(os.environ,PYTHONPATH=f'{tree}/src:{tree}/eval',TMPDIR=str(R.parent/'tmp'),
           SQLITE_TMPDIR=str(R.parent/'tmp'),SEER_SQLWITNESS_SQLITE_SCRATCH=str(R.parent/'tmp'),
           OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',MALLOC_ARENA_MAX='2')
  cmd=['prlimit','--as=6442450944','--','timeout','--signal=TERM','--kill-after=5s',str(budget+240),str(PY),str(Path(__file__).resolve()),
       'worker','--tree',str(tree),'--job',json.dumps(job),'--out',str(out/name)]
  out.mkdir(parents=True,exist_ok=True)
  with (out/(name+'.log')).open('x') as log:
   rc=subprocess.run(cmd,env=env,stdout=log,stderr=subprocess.STDOUT).returncode
  if rc:raise RuntimeError(f'{name} runner exit {rc}; see {out}/{name}.log')
 a,b=(read(out/name/'result.json') for name in ('old','new'))
 diff=differences(output_signature(a),output_signature(b))
 replay=[e for e in b.get('events',[]) if e.get('event')=='inheritance_replay_mismatch']
 write(out/'result.json',dict(passed=not diff,job=job,differences=diff,replay_corrections=replay))
 assert not diff, f'output changed in {job["panel"]}/{job["batch"]}: {len(diff)} differences; freeze prohibited'

def gate(out):
 out=Path(out);out.mkdir(parents=True,exist_ok=False)
 jobs=selection();write(out/'config.json',dict(jobs=jobs,selection='sha256(codex63-v1:batch), frozen roster; LeetCode completed seed 0 only',
    expected={p:sum(j['panel']==p for j in jobs) for p in NAMES},old_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=OLD,text=True).strip()))
 done=[];failures=[];active={};waiting=iter(jobs)
 def run(job):
  assert sha(job['input'])==job['input_sha256'] and sha(job['config'])==job['old_config_sha256']
  dest=out/job['panel']/job['batch']
  cmd=[str(PY),str(Path(__file__).with_name('c63_idle_run.py')),str(PY),str(Path(__file__).resolve()),
       'pair','--job',json.dumps(job),'--out',str(dest)]
  print(json.dumps(dict(event='start',**job)),flush=True)
  return subprocess.call(cmd)
 try:
  # Four CPU-only lanes; each acquires the existing physical-core lease and
  # waits for 128 GiB MemAvailable. The service has a 96 GiB hard limit only.
  with cf.ThreadPoolExecutor(max_workers=4) as pool:
   for _ in range(4):
    job=next(waiting,None)
    if job is not None:active[pool.submit(run,job)]=job
   while active:
    finished,_=cf.wait(active,timeout=15,return_when=cf.FIRST_COMPLETED)
    for future in finished:
     job=active.pop(future)
     try:
      rc=future.result()
      assert rc==0,f'runner exit {rc}'
     except BaseException as exc:failures.append(dict(job=job,error=str(exc)))
     else:done.append(job)
    if not failures:
     while len(active)<4:
      job=next(waiting,None)
      if job is None:break
      active[pool.submit(run,job)]=job
    write(out/'status.json',dict(state='running' if not failures else 'failed_draining',
        active=list(active.values()),passed=len(done),failed=failures,total=len(jobs)))
  assert not failures, f'output parity gate failed in {len(failures)} workloads; no freezing'
  assert len(done)==110
  write(out/'result.json',dict(passed=True,completed=done,freeze_allowed=True))
 except BaseException as exc:
  write(out/'result.json',dict(passed=False,completed=done,failures=failures,error=str(exc),freeze_allowed=False))
  raise
 finally:
  write(out/'status.json',dict(state='terminal',passed=len(done),failed=failures,total=len(jobs),result=str(out/'result.json')))

if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('action',choices=['gate','pair','worker']);ap.add_argument('--tree');ap.add_argument('--job');ap.add_argument('--out',required=True);a=ap.parse_args()
 if a.action=='gate':gate(a.out)
 elif a.action=='pair':pair(json.loads(a.job),a.out)
 else:worker(a.tree,json.loads(a.job),a.out)
