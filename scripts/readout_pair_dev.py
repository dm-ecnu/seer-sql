"""Read the fixed eight development batches through independent pair replay."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import hashlib,json
from pathlib import Path
from scripts.pair_matrix_score import dataset,summarize
BASE=Path((SEER_ROOT))
P=BASE/'pair-dev-v1'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 data=BASE/'datasets/batches/tpcds.jsonl'
 selection=BASE/'gain-policy-wt/runs/gain-dev-v1/dev_ids/tpcds.txt'
 ids=set(selection.read_text().splitlines());assert len(ids)==8
 batches={k:v for k,v in dataset(str(data)).items() if k in ids};assert set(batches)==ids
 inputs=[('pair_seer',0,str(P/'tpcds.s0.pair_seer.v2.jsonl')),('sqlwitness',0,str(P/'tpcds.s0.sqlwitness.jsonl')),('parseval',0,str(P/'tpcds.s0.parseval.jsonl')),('polygon',0,str(P/'tpcds.s0.polygon-classified.jsonl'))]
 for method,seed,path in inputs:
  rows=[json.loads(s) for s in Path(path).read_text().splitlines() if s]
  assert len(rows)==8 and {r['batch_id'] for r in rows}==ids
  assert all(r['seed']==seed and r.get('tool',r.get('method'))==method for r in rows)
 result=summarize(batches,inputs)
 result.update(input_sha256=sha(data),selection_sha256=sha(selection),
  source_sha256={p:sha(Path(p)) for _,_,p in inputs},
  scope='fixed development batches, seed 0 only; not formal five-repeat result')
 assert result['batches']==8 and result['scored_pairs']==80
 assert all(r['complete'] for r in result['runs'].values())
 assert all(v is None for v in result['mean_std'].values())
 dest=P/'tpcds.s0.four-method-score.json'
 with dest.open('x') as f:json.dump(result,f,indent=2)
 print(json.dumps(result,sort_keys=True))
if __name__=='__main__':main()
