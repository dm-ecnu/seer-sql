"""Development trace for shared representative search and fresh SQLite replay."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
from pathlib import Path
import hashlib,json,os
from seer.representative_search import RepresentativeSearch
from seer2.schema import Schema
from seer2.backends import make_backend
B=Path((SEER_ROOT));O=B/'repro/stream-v2-search-v1'
O.mkdir(exist_ok=False)
s=RepresentativeSearch(Schema('CREATE TABLE t(x INTEGER);','sqlite'),'sqlite')
queries=[('0','SELECT COUNT(*) FROM t'),('1','SELECT COUNT(x) FROM t')]
for qid,sql in queries:s.add_query(qid,sql)
a=s.search('0','1',5)
assert a['status']=='candidate' and not a['proof_admitted']
replays=[]
for _ in range(2):
 inst=make_backend('sqlite',s.schema,2).open(a['database'])
 try:
  left,_=inst.run(queries[0][1]);right,_=inst.run(queries[1][1])
  assert left!=right
  replays.append({'left':left,'right':right,'status':'fresh_sqlite_difference'})
 finally:inst.close()
s.add_query('2',queries[0][1])
b=s.search('0','2',5,bounds=(1,2))
assert b['status']=='domain_unsat' and not b['proof_admitted']
assert b['levels'][0]['encoding_reused']
result={'queries':queries+[('2',queries[0][1])],'ddl':s.schema.ddl,'candidate':a,
        'replays':replays,'scoped_unsat':b,'domain_trace':s.trace,'affinity':sorted(os.sched_getaffinity(0)),
        'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
with (O/'trace.json').open('x') as f:json.dump(result,f,sort_keys=True,indent=2)
print(json.dumps({'candidate_k':a['k'],'fresh_sqlite_replays':len(replays),
 'scope':b['scope'],'domain_unsat_k':b['k'],'existing_encoding_reused':b['levels'][0]['encoding_reused'],
 'trace_sha256':hashlib.sha256((O/'trace.json').read_bytes()).hexdigest()}),flush=True)
