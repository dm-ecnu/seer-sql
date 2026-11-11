"""Prepare declared-schema views of published SQLDriller resources.

No checker is implemented here. Invalid witness databases are rejected rather
than repaired. Sampling rows are value seeds, not certificates; SQLite ignores
rows conflicting with declared keys and NOT NULL, and reports all such drops.
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse,collections,fcntl,hashlib,json,os,sqlite3,sys,time
from pathlib import Path

ROOT=Path((SEER_ROOT))
sys.path.insert(0,str(ROOT/'schema-adapter-wt/eval'))
from seereval.schema import Schema,q_ident
from seereval.engine import SQLiteEngine
from seereval.validator import check_db

COMMIT='651401923af77db68f7f32b060f57da1b8e1e444'
VERSION=1

def read_instance(path,schema,limit=None):
 con=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
 try:
  tables={r[0].lower():r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  data={};missing=[]
  for t in schema.tables:
   if t.name.lower() not in tables:
    data[t.name]=[];missing.append(t.name);continue
   sql='SELECT * FROM '+q_ident(tables[t.name.lower()],'sqlite')
   if limit is not None:sql+=' LIMIT '+str(int(limit))
   rows=[]
   for r in con.execute(sql):
    keys={k.lower():k for k in r.keys()}
    rows.append({c['name']:r[keys[c['name'].lower()]] for c in t.columns})
   data[t.name]=rows
  return data,missing
 finally:con.close()


def write_database(path,schema,instance,sampling=False):
 eng=SQLiteEngine(schema)
 seen=accepted=0
 try:
  if sampling:
   eng.con.execute('PRAGMA foreign_keys=OFF')
   for t in schema.tables:
    cols=','.join(q_ident(c['name'],'sqlite') for c in t.columns)
    sql='INSERT OR IGNORE INTO '+q_ident(t.name,'sqlite')+' ('+cols+') VALUES ('+','.join('?' for _ in t.columns)+')'
    for row in instance.get(t.name,[]):
     seen+=1
     cur=eng.con.execute(sql,eng._row_values(t,row))
     accepted+=cur.rowcount
  else:eng.load(instance)
  path.parent.mkdir(parents=True,exist_ok=True)
  temp=path.with_name(path.name+f'.building-{os.getpid()}')
  target=sqlite3.connect(temp)
  try:
   # These are reproducible cache files; publish only after a complete backup.
   target.execute('PRAGMA journal_mode=MEMORY')
   target.execute('PRAGMA synchronous=OFF')
   eng.con.backup(target)
  finally:target.close()
  os.replace(temp,path)
 finally:eng.close()
 return {'source_rows':seen,'sample_rows':accepted,'sample_rows_dropped':seen-accepted} if sampling else {}


def publish_index(result,schema):
 key=hashlib.sha256(json.dumps(schema.to_json(),sort_keys=True).encode()).hexdigest()
 index=ROOT/'sqldriller/resources/declared/index';index.mkdir(parents=True,exist_ok=True)
 temp=index/f'{key}.{os.getpid()}.tmp'
 temp.write_text(json.dumps({'resource_view':str(Path(result['schema_path']).parent/'resource.json'),'resource_key':result['resource_key']})+'\n')
 temp.replace(index/f'{key}.json')
 return result


def prepare(schema,family):
 source=ROOT/f'sqldriller/resources/native/{family}/schema_for_sample_values'/schema.name/(schema.name+'.sqlite')
 suites=ROOT/'sqldriller/resources'/('database' if family=='spider' else 'dbs/fuzz/bird/dev/original')/schema.name
 assert source.is_file(),source
 paths=sorted(suites.glob('*.sqlite'));assert paths,suites
 key_data={'schema':schema.to_json(),'commit':COMMIT,'adapter_version':VERSION,'adapter_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'family':family,'sample_source':str(source),'sample_size':source.stat().st_size,'sample_mtime_ns':source.stat().st_mtime_ns,'suite_files':[(p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in paths]}
 key=hashlib.sha256(json.dumps(key_data,sort_keys=True).encode()).hexdigest()
 base=ROOT/'sqldriller/resources/declared'/key;base.mkdir(parents=True,exist_ok=True)
 lock=(base/'prepare.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX)
 meta=base/'resource.json'
 if meta.exists():return publish_index(json.loads(meta.read_text()),schema)
 started=time.monotonic();cpu=time.process_time()
 data,missing=read_instance(source,schema,limit=1000)
 sample=base/'data'/family/'schema_for_sample_values'/schema.name/(schema.name+'.sqlite')
 sampling=write_database(sample,schema,data,sampling=True)
 dest=base/'suite'/schema.name;dest.mkdir(parents=True,exist_ok=True)
 counts=collections.Counter();records=[]
 for path in paths:
  entry={'source':str(path)}
  try:
   inst,absent=read_instance(path,schema)
   verdict=check_db(schema,'sqlite',inst,{},timeout_s=30,also_bare=False)
   entry.update(status=verdict['status'],violations=verdict['violations'],completed_empty_tables=absent)
   if verdict['status']=='valid':
    out=dest/path.name
    write_database(out,schema,inst)
    entry['prepared']=str(out)
  except Exception as error:entry.update(status='resource_error',error=f'{type(error).__name__}: {error}')
  counts[entry['status']]+=1;records.append(entry)
 result={'resource_key':key,'upstream_commit':COMMIT,'family':family,'schema_name':schema.name,'schema_path':str(base/'schema.json'),'schema_db_base':str(sample.parent.parent),'suite':str(dest.parent),'source_files':len(paths),'counts':dict(counts),'sampling':sampling,'sampling_missing_tables':missing,'wall_s':time.monotonic()-started,'cpu_s':time.process_time()-cpu,'resources_ready':bool(counts['valid']),'databases':records,'cost_scope':'offline_input_preparation; not included in online checker CPU'}
 (base/'schema.json').write_text(json.dumps(schema.to_json(),indent=2)+'\n')
 tmp=base/'resource.tmp';tmp.write_text(json.dumps(result,indent=2)+'\n');tmp.replace(meta)
 return publish_index(result,schema)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--schema',type=Path,required=True);p.add_argument('--family',choices=['bird','spider'],required=True);a=p.parse_args()
 result=prepare(Schema.load(a.schema),a.family)
 print(json.dumps({k:v for k,v in result.items() if k!='databases'}),flush=True)
