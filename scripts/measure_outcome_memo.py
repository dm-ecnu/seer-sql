"""Paired comparison-kernel measurements on the frozen public LeetCode dev batches.

Acquire outputs using the unchanged native executor and a saved witness first.
Both timed kernels receive the same complete outputs. No synthesis/EX gain is
claimed by this isolated measurement. Error/timeout batches stay in the output.
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse
from collections import Counter
import hashlib
import importlib.metadata
import itertools
import json
import os
from pathlib import Path
import resource
import signal
import statistics
import sys
import subprocess
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from scripts import run_pair_seer as R
from seer.core import split_class_n
from seer.outcome_memo import separated_pairs

BASE=Path((SEER_ROOT));ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'runs/outcome-memo-kernel-v1'
DATA=BASE/'datasets/batches/leetcode_batches.jsonl'
IDS=BASE/'gain-policy-wt/runs/gain-dev-v1/dev_ids/leetcode_batches.txt'
SOURCE=BASE/'seer-mx2-wt/runs/server/mx_seer'
class BatchTimeout(BaseException):pass

def alarm(*_):raise BatchTimeout('600-second acquisition/measurement cap')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def digest(pairs):return hashlib.sha256(json.dumps(pairs,separators=(',',':')).encode()).hexdigest()
def uptime():return subprocess.check_output(['uptime'],text=True).strip()

def prepare():
 assert os.sched_getaffinity(0)=={159},'measure on the declared CPU only'
 selected=set(IDS.read_text().splitlines());assert len(selected)==7
 batches={b['batch_id']:b for b in map(json.loads,DATA.read_text().splitlines()) if b['batch_id'] in selected}
 assert set(batches)==selected
 original={};source_files=sorted(SOURCE.glob('leetcode_batches.seer.s0.*.jsonl'))
 for p in source_files:
  for line in p.read_text().splitlines():
   row=json.loads(line)
   if row['batch_id'] in selected:
    assert row['batch_id'] not in original,'duplicate frozen source'
    original[row['batch_id']]=row
 assert set(original)==selected
 files=[DATA,IDS,Path(__file__),*source_files]
 files += list((ROOT/'src/seer').glob('*.py'))+list((ROOT/'src/seer2').glob('*.py'))+[Path(R.__file__)]
 files += [(DATA.parent/b['schema_path']).resolve() for b in batches.values()]
 versions={name:importlib.metadata.version(name) for name in ['duckdb','sqlglot','z3-solver','pyarrow']}
 config={'type':'protocol','development_only':True,'scope':'native saved-witness output comparison kernel; not end-to-end synthesis or quality',
         'selected':{k:len(v['queries']) for k,v in sorted(batches.items())},'repeats':5,'cache_entries':4096,
         'batch_cap_s':600,'query_timeout_s':10,'process_as_gib':8,'cpu':159,'runtime_cap_s':7200,
         'thread_environment':{'SLURM_CPUS_ON_NODE':os.environ.get('SLURM_CPUS_ON_NODE')},
         'versions':versions,'source_sha256':{str(p):sha(p) for p in sorted(set(files))}}
 assert config['thread_environment']=={'SLURM_CPUS_ON_NODE':'1'}
 return batches,original,config

def measure(batch,old):
 if not old.get('certs') or not old.get('n_valid_dbs'):
  return {'status':'missing_witness','error':'fixed source has no validated saved witness'}
 started=time.monotonic()
 schema=R.Schema.load(str(DATA.parent/batch['schema_path']))
 dialect=batch['dialect']
 s=R.SeerSchema(';\n'.join(R.to_ddl(schema,dialect,deferrable=False))+';',R.DIALECT[dialect],inclusions=R.sigma_extra(schema))
 queries=[(q['qid'],R.normalize_sql(q['sql'],dialect)) for q in batch['queries']]
 sr=R.PairSeer(s,queries,dialect=R.DIALECT[dialect],backend=R.BACKEND[R.DIALECT[dialect]],
               fast=False,symbolic=False,budget=600,seed=0,qtimeout=10)
 db=R.from_jsonable(old['certs'][0]['db'])
 violations=s.violations(db);assert not violations,violations[:3]
 pairs=sr.pending_pairs();live=set(q for p in pairs for q in p)
 qs=[q for q in sr.queries if q in live]
 inst=sr.backend.open(db)
 try:outs=sr._exec_all(inst,db,qs)
 finally:inst.close()
 acquired=time.monotonic()-started
 trials=[];expected=None
 for repeat in range(5):
  for method in (['plain','memo'] if repeat%2==0 else ['memo','plain']):
   wall,cpu=time.perf_counter(),time.process_time()
   if method=='plain':
    result=[p for p in pairs if len(split_class_n(list(p),outs))==2]
    stats={'comparison_pairs':len(pairs),'comparison_evaluations':len(pairs),'comparison_cache_hits':0,'comparison_cache_size':0}
   else:result,stats=separated_pairs(pairs,outs,maxsize=4096)
   cpu=time.process_time()-cpu;wall=time.perf_counter()-wall
   if expected is None:expected=result
   assert result==expected,'memo changed the original pair predicate'
   trials.append({'repeat':repeat,'method':method,'wall_s':wall,'cpu_s':cpu,'split_pairs':len(result),**stats})
 metrics={}
 for method in ('plain','memo'):
  rows=[r for r in trials if r['method']==method]
  metrics[method]={field:{'mean':statistics.mean(r[field] for r in rows),'sample_std':statistics.stdev(r[field] for r in rows)} for field in ('wall_s','cpu_s','comparison_evaluations')}
 return {'status':'ok','input_queries':len(queries),'invalid_queries':sorted(sr.invalid),
         'live_queries':len(qs),'pairs':len(pairs),'native_outcomes':dict(Counter(o.kind for o in outs.values())),
         'native_query_output_evaluations':sr.stats.get('executions',0),
         'native_order_probe_evaluations':sr.stats.get('executions_order_probe',0),'acquisition_wall_s':acquired,
         'saved_database_index':old['certs'][0]['idx'],'pairs_sha256':digest(expected),
         'trials':trials,'metrics':metrics,'all_pair_results_identical':True}

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--plan',action='store_true');a=ap.parse_args()
 resource.setrlimit(resource.RLIMIT_AS,(8*2**30,8*2**30))
 batches,original,config=prepare()
 if a.plan:
  print(json.dumps({'protocol':config,'selected_batches':len(batches)},sort_keys=True));return
 OUT.mkdir(parents=True,exist_ok=True)
 dest=OUT/'results.jsonl';assert not dest.exists(),'refuse to overwrite a measurement attempt'
 (OUT/'run.pid').write_text(str(os.getpid())+'\n')
 signal.signal(signal.SIGALRM,alarm)
 with dest.open('x') as f:
  f.write(json.dumps(config,sort_keys=True)+'\n');f.flush()
  for bid in sorted(batches):
   start=uptime();print(json.dumps({'state':'acquiring_native_outputs','batch_id':bid,'uptime':start}),flush=True)
   signal.alarm(600)
   try:row=measure(batches[bid],original[bid])
   except BatchTimeout as e:row={'status':'timeout','error':str(e)}
   except Exception as e:row={'status':'error','error':type(e).__name__+': '+str(e)[:400]}
   finally:signal.alarm(0)
   row.update(batch_id=bid,uptime_start=start,uptime_end=uptime())
   assert all(sha(p)==h for p,h in config['source_sha256'].items()),'frozen mechanism source changed'
   f.write(json.dumps(row,sort_keys=True)+'\n');f.flush()
   print(json.dumps({k:row[k] for k in ['batch_id','status','pairs','metrics','error'] if k in row}),flush=True)
 (OUT/'DONE').write_text('Fixed seven development batches attempted; inspect all failures. Not an end-to-end speedup claim.\n')
if __name__=='__main__':main()
