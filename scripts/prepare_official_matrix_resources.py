"""Server-only input preparation for the frozen Spider/BIRD matrix, no evaluation."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import json,sys,time
from pathlib import Path
R=Path((SEER_ROOT));W=R/'schema-adapter-wt'
sys.path[:0]=[str(W/'eval'),str(W/'eval/tools')]
from seereval.schema import Schema
from sqldriller_resource_view import prepare
started=time.monotonic(); cpu=0.; missing=[];n=0
for family,expected in [('spider',20),('bird',11)]:
 files=sorted((R/f'datasets/batches/schemas/{family}').glob('*.json'))
 assert len(files)==expected,(family,len(files),expected)
 for path in files:
  result=prepare(Schema.load(path),family);n+=1;cpu+=result['cpu_s']
  if not result['resources_ready']:missing.append((family,result['schema_name']))
  print(json.dumps({'family':family,'schema':result['schema_name'],'ready':result['resources_ready'],'counts':result['counts'],'resource_key':result['resource_key'],'preparation_wall_s':result['wall_s'],'preparation_cpu_s':result['cpu_s']}),flush=True)
print(json.dumps({'phase':'input_resources_only','schemas':n,'missing':missing,'resource_preparation_cpu_s':cpu,'this_invocation_wall_s':time.monotonic()-started}),flush=True)
sys.exit(1 if missing else 0)
