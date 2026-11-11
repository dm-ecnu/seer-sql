"""Read the complete fixed public batch through independent pair witness replay."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import hashlib
import json
from pathlib import Path
from scripts.pair_matrix_score import dataset,score_record
BASE=Path((SEER_ROOT))
OUT=BASE/'pair-replay-shared-v2'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 assert (OUT/'exit.code').read_text().strip()=='0','native batch process is not successfully terminal'
 path=OUT/'leetcode-1084.s0.jsonl';source=sha(path)
 rows=[json.loads(s) for s in path.read_text().splitlines() if s]
 assert len(rows)==1;row=rows[0]
 assert row['batch_id']=='leetcode-1084' and row['n_queries']==889 and row['seed']==0 and row['budget']==600
 assert row['tool']=='pair_seer' and row['state_model']=='pair_evidence' and not row['runner_error']
 contract=row['comparison_contract'];assert contract['shared_pair_replay'] is True and not contract['outcome_memo']
 assert all(sha(p)==digest for p,digest in contract['files'].items()),'method source changed'
 batches=dataset(str(BASE/'datasets/batches/leetcode_batches.jsonl'));batch=batches[row['batch_id']]
 assert len(batch['queries'])==889
 outcomes,cost=score_record(batch,row)
 assert sha(path)==source,'native output changed during validation'
 from collections import Counter
 result={'scope':'one fixed development batch; not five-repeat main results',
         'source_sha256':source,'input_queries':889,'pair_graph_edges':len(row['pairs']),
         'independently_validated_graph_splits':row['n_genuine_pairs'],
         'scored_reference_pairs':len(outcomes),'reference_outcomes':dict(Counter(outcomes.values())),
         'runner_wall_s':row['runner_wall'],'timing':row['timing'],'stats':row['stats'],
         'cpu_scope':cost,'late_instances':len(row['late_instances']),
         'certificate_databases':row['n_dbs'],'rejected_claims':len(row['rejected_claims'])}
 assert result['pair_graph_edges']==889*888//2
 dest=OUT/'large-independent-score.json'
 with dest.open('x') as f:json.dump(result,f,indent=2);f.write('\n')
 print(json.dumps(result,sort_keys=True))
if __name__=='__main__':main()
