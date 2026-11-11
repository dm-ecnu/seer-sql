import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import json,sqlite3,sys
from pathlib import Path
R=Path((SEER_ROOT));W=R/'schema-adapter-wt'
sys.path[:0]=[str(W/'eval'),str(W/'eval/tools')]
from seereval import tools
from seereval.schema import Schema
from seereval.validator import classify_pair
from sqldriller_resource_view import prepare
D=R/'repro/sqldriller/datasets';row=json.loads((D/'sqld_spider_pairs.jsonl').read_text().splitlines()[0]);s=Schema.load(D/row['schema_path'])
v=prepare(s,'spider');assert v['resources_ready'],v
sample=Path(v['schema_db_base'])/s.name/(s.name+'.sqlite')
c=sqlite3.connect(sample)
pk=sorted((r[5],r[1]) for r in c.execute('PRAGMA table_info(singer_in_concert)') if r[5]);c.close()
assert [col.lower() for _,col in pk]==[col.lower() for col in s.table('singer_in_concert').primary_key]
req=tools.build_request('sqldriller_official',s,'sqlite',[q['sql'] for q in row['queries']],60,0)
# schema_db_base=<view>/data/spider/schema_for_sample_values, so derive from metadata's schema path.
req['resource_view']=str(Path(v['schema_path']).parent/'resource.json')
out=tools.call('sqldriller_official',req,hard_s=60)
assert out.get('resource_contract')=='declared_schema',out
if out.get('instance') is not None:
 out['common_validator']=classify_pair(s,'sqlite',out.pop('instance'),*[q['sql'] for q in row['queries']],timeout_s=30)
 assert out['common_validator']['class']=='genuine',out
assert out['verdict']=='neq',out
print(json.dumps({'test':'declared_schema_native_checker','sample_pk':pk,'suite_counts':v['counts'],'sampling':v['sampling'],'result':out},default=str),flush=True)
