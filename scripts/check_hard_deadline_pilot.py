"""Read the exact parent receipts, then independently replay credited databases."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import collections,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'eval'),str(ROOT/'src')]
from pair_matrix_score import dataset,load_records,score_record

B=Path((SEER_ROOT))
D=B/'datasets/batches/public-batch-v1'
O=B/'repro/public-bird-hard-deadline-pilot-v1'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def main():
    bid=(D/'pilot-dev.ids').read_text().strip()
    assert bid in (D/'dev.ids').read_text().splitlines()
    assert bid not in (D/'test.ids').read_text().splitlines()
    batch=dataset(str(D/'bird_public.jsonl'))[bid]
    readout={'scope':'single predeclared development input; hard receipt integration, not quality/cost comparison','batch_id':bid,'methods':{}}
    for method in ('pair_seer','sqlwitness','parseval'):
        path=O/(method+'.jsonl');rows,contract=load_records(str(path));assert set(rows)=={bid}
        row=rows[bid];assert row['tool']==method and row['budget']==5 and row['seed']==0
        assert contract['hard_parent_receipt_deadline'] and contract['fresh_common_dbms_runs']==2
        assert all(sha(p)==h for p,h in contract['files'].items())
        assert len(row['pairs'])==55
        journal=Path(row['journal']);receipts=row['accepted_receipts']
        saved=[json.loads(l) for l in (journal/'receipts.jsonl').read_text().splitlines()]
        assert saved==receipts
        credited={}
        for receipt in receipts:
            assert 0<=receipt['received_s']<row['budget']
            event_path=journal/receipt['file'];assert sha(event_path)==receipt['sha256']
            event=json.loads(event_path.read_text())
            if event['kind']=='certificate':
                assert sha(journal/event['database'])==event['database_sha256']
                for edge in event['pairs']:
                    pair=frozenset(edge);assert pair not in credited;credited[pair]=receipt['received_s']
        assert {frozenset((p['a'],p['b'])) for p in row['pairs'] if p['verdict']=='split'}==set(credited)
        assert all(p['accepted_s']==credited[frozenset((p['a'],p['b']))] for p in row['pairs'] if p['verdict']=='split')
        assert row['n_genuine_pairs']==len(credited)
        outcomes,_=score_record(batch,row)
        assert not set(outcomes.values())&{'missing','invalid_cert','crash'}
        readout['methods'][method]={'output_sha256':sha(path),'source_status':row['source_status'],'worker_error':row['worker_error'],'genuine_pairs':len(credited),'independent_outcomes':dict(collections.Counter(outcomes.values())),'last_accept_s':row['last_accept_s'],'wall_s':row['wall_secs'],'cleanup_s':row['cleanup_s'],'cpu_complete':row['cpu_complete'],'owned_native_groups':row['owned_native_groups']}
    print(json.dumps(readout,sort_keys=True))

if __name__=='__main__':main()
