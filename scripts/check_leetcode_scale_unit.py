"""Independently replay the whole accepted database set for one completed unit."""
import argparse,collections,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'eval'),str(ROOT/'src')]
from pair_matrix_score import dataset,load_records,score_record
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
    ap=argparse.ArgumentParser();ap.add_argument('output');a=ap.parse_args()
    output=Path(a.output);rows,contract=load_records(str(output/'raw.jsonl'))
    assert len(rows)==1
    bid,row=next(iter(rows.items()))
    inputs=ROOT.parent/'datasets/batches/leetcode-scale-v1'
    batch=dataset(str(inputs/'leetcode_scale.jsonl'))[bid]
    n=len(batch['queries']);assert n==batch['meta']['n']
    assert n in (10,25,50,100,200,500) and batch['dialect']=='mysql'
    assert row['seed'] in range(5) and row['budget']==60
    assert contract['hard_parent_receipt_deadline'] and contract['fresh_common_dbms_runs']==2
    assert all(sha(p)==h for p,h in contract['files'].items())
    assert not row.get('runner_error') and not row.get('worker_error'),row.get('worker_error')
    assert row['source_status'] in ('deadline','completed')
    assert len(row['pairs'])==n*(n-1)//2
    assert row['cpu_host_complete'] and row['cpu_scope']=='exclusive_host_cgroup'
    assert not row['cpu_complete'],'external MySQL CPU cannot be complete'
    assert row['service_cleanup']['verified']
    assert all(r['verified'] for r in row['external_backend_cleanup'])
    assert row['service_peak_memory_bytes']>0
    journal=Path(row['journal'])
    receipts=[json.loads(l) for l in (journal/'receipts.jsonl').read_text().splitlines()]
    assert receipts==row['accepted_receipts']
    credited={}
    for receipt in receipts:
        assert 0<=receipt['received_s']<60
        event_path=journal/receipt['file'];assert sha(event_path)==receipt['sha256']
        event=json.loads(event_path.read_text())
        if event['kind']=='certificate':
            assert sha(journal/event['database'])==event['database_sha256']
            for edge in event['pairs']:
                pair=frozenset(edge);assert pair not in credited
                credited[pair]=receipt['received_s']
    splits={frozenset((p['a'],p['b'])) for p in row['pairs'] if p['verdict']=='split'}
    assert splits==set(credited) and len(splits)==row['n_genuine_pairs']
    assert all(p['accepted_s']==credited[frozenset((p['a'],p['b']))] for p in row['pairs'] if p['verdict']=='split')
    metrics=row['call_metrics'];assert not metrics['partial_line']
    entries=metrics['entries']
    runtime=None
    if row['tool']=='pair_seer':
        runtime=json.loads((journal/'duckdb-runtime.json').read_text())
        assert runtime['threads_after_default_close']<16
        assert any(e['kind']=='coverage' and e['pid']==runtime['pid'] and e['role']=='host:pair_seer' for e in entries)
    assert any(e['kind']=='coverage' and e.get('component')=='host' for e in entries)
    returns=collections.Counter();reasons=collections.Counter()
    for entry in entries:
        if entry['kind']=='generation_api_finished':
            path=journal/entry['result_file'];assert sha(path)==entry['result_sha256']
            native=json.loads(path.read_text())
            assert 'entry_metrics.py' not in native.get('trace',''),'observer failed'
            returns[native.get('verdict')]+=1
            if native.get('reason'):reasons[str(native['reason'])[:250]]+=1
    outcomes,_=score_record(batch,row)
    assert len(outcomes)==n*(n-1)//2
    assert not set(outcomes.values())&{'missing','invalid_cert','crash'}
    checked={'batch_id':bid,'parent_batch_id':batch['meta']['parent_batch_id'],'split':batch['meta']['split'],
        'n':n,'seed':row['seed'],'method':row['tool'],'budget':60,'source_status':row['source_status'],
        'raw_sha256':sha(output/'raw.jsonl'),'independent_outcomes':dict(collections.Counter(outcomes.values())),
        'entry_counts':metrics['counts'],'native_verdicts':dict(returns),'native_reasons':dict(reasons),
        'duckdb_runtime':runtime,'accepted_databases':len(row['dbs']),'last_accept_s':row['last_accept_s'],'wall_secs':row['wall_secs'],
        'host_cpu_secs':row['cpu_secs'],'host_cpu_complete':row['cpu_host_complete'],'total_cpu_complete':row['cpu_complete'],
        'service_peak_memory_bytes':row['service_peak_memory_bytes'],'memory_scope':row['service_peak_memory_scope'],
        'external_backend_cleanup':row['external_backend_cleanup'],'owned_service_cleanup':row['service_cleanup']}
    data=json.dumps(checked,sort_keys=True)+'\n';dest=output/'checked.json'
    if dest.exists():assert dest.read_text()==data
    else:
        with dest.open('x') as stream:stream.write(data)
    print(data,end='')
if __name__=='__main__':main()
