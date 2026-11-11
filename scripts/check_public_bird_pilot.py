"""Independently replay the one declared development smoke batch."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'eval'),str(ROOT/'src')]
from pair_matrix_score import dataset,scored_pairs,load_records,score_record

B=Path((SEER_ROOT))
D=B/'datasets/batches/public-batch-v1'
O=B/'repro/public-bird-adapter-pilot-v1'
info=json.loads((D/'runner-input.json').read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
assert sha(D/'bird_public.jsonl')==info['dataset_sha256']
bid=(D/'pilot-dev.ids').read_text().strip()
assert bid==info['pilot_development_batch']
assert bid in (D/'dev.ids').read_text().splitlines()
assert bid not in (D/'test.ids').read_text().splitlines()
batch=dataset(str(D/'bird_public.jsonl'))[bid]
expected=scored_pairs(batch)
readout={'scope':'single predeclared development input integration; not comparative quality/cost','dataset_sha256':info['dataset_sha256'],'batch_id':bid,'queries':len(batch['queries']),'eligible_pairs':len(expected),'methods':{}}
for method in ('pair_seer','sqlwitness','parseval'):
    path=O/(method+'.jsonl')
    records,contract=load_records(str(path))
    assert set(records)=={bid}
    row=records[bid]
    assert row['tool']==method and row['seed']==0 and row['budget']==5
    assert not row['runner_error'],(method,row['runner_error'])
    assert all(sha(p)==h for p,h in contract['files'].items())
    assert len(row['pairs'])==55
    assert {frozenset((p['a'],p['b'])) for p in row['pairs']}=={frozenset((a['qid'],b['qid'])) for i,a in enumerate(batch['queries']) for b in batch['queries'][i+1:]}
    outcomes,detail=score_record(batch,row)
    assert set(outcomes)==set(expected)
    assert all(v not in ('invalid_cert','missing','crash') for v in outcomes.values()),(method,outcomes)
    readout['methods'][method]={'output_sha256':sha(path),'runner_error':row['runner_error'],'raw_genuine_pairs':row.get('n_genuine_pairs'),'independent_outcomes':{v:sum(x==v for x in outcomes.values()) for v in sorted(set(outcomes.values()))},'wall_s':row['runner_wall'],'cpu_complete':row.get('cpu_complete'),'n_dbs':row.get('n_dbs'),'source_files_checked':len(contract['files'])}
print(json.dumps(readout,sort_keys=True))
