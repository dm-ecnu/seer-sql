"""Server-only opt-in integration: official checker, common validator and CPU scoring.

The final case deliberately aborts the worker to verify incomplete accounting;
none of these smoke records are paper results.
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import copy,json,sys
from pathlib import Path
from types import SimpleNamespace
R=Path((SEER_ROOT));W=R/'schema-adapter-wt'
sys.path[:0]=[str(W/'eval'),str(W/'scripts'),str(W/'eval/tools')]
from seereval import tools,shell
from sqldriller_resource_view import prepare
from seereval.schema import Schema
from run_baseline import run_batch
from matrix_score import score_run
from unittest.mock import patch
root=R/'repro/sqldriller/datasets'
row=json.loads((root/'sqld_spider_pairs.jsonl').read_text().splitlines()[0])
a=SimpleNamespace(tool='sqldriller_official',budget=60,per_call=60,seed=0,restart=False)
s=Schema.load(root/row['schema_path'])
view=prepare(s,'spider');assert view['resources_ready']
for name in ('native_neq','native_eq','forced_abort'):
 b=copy.deepcopy(row);b['batch_id']=name
 if name=='native_eq':b['queries'][1]['sql']=b['queries'][0]['sql']
 if name=='forced_abort':
  def aborted(tool,st,qs,timeout,seed,**kwargs):
   req=tools.build_request(tool,st.schema,st.dialect,[st.sql[q] for q in qs],timeout,seed)
   return tools.call(tool,req,hard_s=0.02)
  with patch.object(shell,'_tool_call',aborted):result=run_batch(b,a,str(root))
 else:result=run_batch(b,a,str(root))
 assert not result['runner_error'],result
 ids=[q['qid'] for q in b['queries']]
 ds={name:{'ref':ids[0],'ids':ids,'pairs':[(ids[0],ids[1])],'author_neq':set()}}
 outcomes,pb=score_run(ds,{name:result},a.tool)
 if name=='native_neq':
  assert result['counts'].get('genuine')==1,result
  c=result['calls'][0]
  assert c['docker_cpu_s']>0 and c['cpu']>=c['host_cpu']+c['docker_cpu_s']-0.002,c
  assert result['cpu_complete'] and pb[0]['cpu'] is not None
 elif name=='native_eq':
  assert result['calls'][0]['stage']=='sqlsolver_eq',result
  assert result['cpu_complete']
 else:
  assert not result['cpu_complete'] and result['incomplete_cpu_calls']==1,result
  assert pb[0]['cpu'] is None and pb[0]['cpu_lower_bound'] is not None,pb
 print(json.dumps({'test':name,'record':result,'scored_outcome':list(outcomes.values()),'scored_cpu':pb[0]['cpu'],'scored_cpu_lower_bound':pb[0]['cpu_lower_bound']},default=str),flush=True)
