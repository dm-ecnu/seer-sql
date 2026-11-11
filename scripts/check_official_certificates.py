"""Preserve native SQLDriller certificates and use the existing common validator."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse,collections,hashlib,json,re,resource,shutil,sqlite3,sys,time
from pathlib import Path
R=Path((SEER_ROOT))
eval_root=R/('schema-adapter-wt/eval' if 'native-fixed' in sys.argv else 'seer-mx2-wt/eval')
sys.path.insert(0,str(eval_root))
from seereval.schema import Schema,from_ddl
from seereval.validator import classify_pair
resource.setrlimit(resource.RLIMIT_AS,(4*1024**3,resource.RLIM_INFINITY))
p=argparse.ArgumentParser(); p.add_argument('benchmark',choices=['spider','bird']); p.add_argument('--run-stem'); p.add_argument('--schema-source',choices=['frozen','native','native-fixed'],default='frozen'); a=p.parse_args(); b=a.benchmark
run=a.run_stem or b
assert run.replace('-','').replace('_','').isalnum(),run
root=R/'repro/official'; source=root/f'{run}.jsonl'; stem=run+('.'+a.schema_source if a.schema_source!='frozen' else ''); out=root/f'{stem}.checked.jsonl'
rows=[json.loads(l) for l in source.read_text().splitlines()]
# A finished file is not a healthy reproduction if native code swallowed I/O
# failures. Preserve valid witnesses while recording the run-level incompleteness.
transport_pattern=re.compile(r"connection (error|refused)|failed to (connect|establish)|no route|gateway unreachable",re.I)
transport_known={};transport_bad={}
for record in rows:
 key=record["batch_id"]
 if "infra_transport_error" in record:
  transport_known[key]=True;transport_bad[key]=bool(record["infra_transport_error"])
 else:
  logfile=root/(run+".logs")/(key+".log")
  transport_known[key]=logfile.is_file()
  transport_bad[key]=bool(transport_pattern.search(logfile.read_text(errors="replace"))) if logfile.is_file() else False
inputs={r['batch_id']:r for r in map(json.loads,(R/f'repro/sqldriller/datasets/sqld_{b}_pairs.jsonl').read_text().splitlines())}
previous=[json.loads(l) for l in out.read_text().splitlines()] if out.exists() else []
done={r['batch_id'] for r in previous}; assert len(done)==len(previous)
certdir=root/'certificates';certdir.mkdir(exist_ok=True)
with out.open('a') as f:
 for rec in rows:
  key=rec['batch_id']
  if key in done: continue
  pair=inputs[key]
  result={'batch_id':key,'benchmark':b,'run_stem':run,'schema_source':a.schema_source,'schema_parser_sha256':hashlib.sha256((eval_root/'seereval/schema.py').read_bytes()).hexdigest(),'native_verdict':rec['verdict'],'label':pair['meta']['label'],'wall_s':rec['wall_s'],'native_exception_logged':rec['native_exception_logged']}
  path=rec.get('certificate')
  if rec['verdict']=='NEQ' and path:
   try:
    path=Path(path); digest=hashlib.sha256(path.read_bytes()).hexdigest(); dest=certdir/f'{digest}.sqlite'
    if not dest.exists(): shutil.copyfile(path,dest)
    result.update(certificate=str(dest),sha256=digest)
    if a.schema_source.startswith('native'):
        name=pair['meta']['db_id']
        ddl=R/f'sqldriller/upstream/data/{b}/schemas/{name}.sql'
        schema=from_ddl(name,ddl.read_text(),dialect='sqlite',source=str(ddl))
        assert not schema.unsupported, schema.unsupported
    else:
        schema=Schema.load(R/'repro/sqldriller/datasets'/pair['schema_path'])
    c=sqlite3.connect(dest.as_uri()+'?mode=ro',uri=True); c.row_factory=sqlite3.Row
    instance={}
    present={r[0].lower() for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing=[]
    for t in schema.tables:
     if t.name.lower() not in present:
      instance[t.name]=[]
      missing.append(t.name)
      continue
     q='SELECT * FROM "'+t.name.replace('"','""')+'"'
     values=[]
     for row in c.execute(q):
      index={k.lower():k for k in row.keys()}
      values.append({col['name']:row[index[col['name'].lower()]] for col in t.columns})
     instance[t.name]=values
    c.close()
    result['completed_empty_tables']=missing
    result['checker_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    checked=classify_pair(schema,'sqlite',instance,pair['queries'][0]['sql'],pair['queries'][1]['sql'],timeout_s=30)
    result['checked']=checked
   except Exception as e:
    result['validation_error']=f'{type(e).__name__}: {e}'
  f.write(json.dumps(result,default=str)+'\n'); f.flush()
allrows=[json.loads(l) for l in out.read_text().splitlines()]
summary={'benchmark':b,'schema_source':a.schema_source,'checked_rows':len(allrows),'expected':len(inputs),'complete':len(allrows)==len(inputs),'native_verdicts':dict(collections.Counter(r['native_verdict'] for r in allrows)),'certificate_classifications':dict(collections.Counter(r.get('checked',{}).get('class','validation_error' if 'validation_error' in r else 'no_certificate') for r in allrows))}
summary["transport_affected_rows"]=sum(transport_bad.values())
summary["transport_unverified_rows"]=sum(not value for value in transport_known.values())
summary["complete_without_transport_outage"]=summary["complete"] and not summary["transport_affected_rows"] and not summary["transport_unverified_rows"]
for label in ('official_neq','official_none'):
 group=[r for r in allrows if r['label']==label]
 summary[label]={'n':len(group),'genuine':sum(r.get('checked',{}).get('class')=='genuine' for r in group),'native_neq':sum(r['native_verdict']=='NEQ' for r in group)}
(root/f'{stem}.checked.summary.json').write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps(summary),flush=True)
