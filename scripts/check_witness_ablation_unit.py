"""Original whole-graph DBMS replay plus independent certificate-scope checks."""
import argparse,hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
NO_REUSE='no_cross_pair_certificate_reuse'
VARIANTS=('full',NO_REUSE)
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def verify_witness_scope(row):
    variant=row['ablation'];assert variant in VARIANTS
    reuse=variant=='full'
    assert row['witness_reuse'] is reuse and row['comparison_contract']['witness_reuse'] is reuse
    assert row['encoding_configuration']=={'share_query_encoding':True,'encode_sigma':True}
    entries=row['call_metrics']['entries'];journal=Path(row['journal'])
    proposals=[e for e in entries if e['kind']=='witness_proposal_scope']
    finished=[e for e in entries if e['kind']=='witness_proposal_finished']
    validations=[e for e in entries if e['kind']=='witness_validation_scope']
    by_id={e['proposal_id']:e for e in proposals}
    ended={e['proposal_id']:e for e in finished}
    assert len(by_id)==len(proposals) and len(ended)==len(finished)
    assert all(type(i) is int and i>=0 for i in by_id)
    for e in proposals:
        assert e['cross_pair_certificate_reuse'] is reuse
        if not reuse:assert e['pair_count']==1 and len(set(e['target_pair']))==2
    for e in finished:
        assert e['proposal_id'] in by_id and by_id[e['proposal_id']]['t']<=e['t']
        assert e['cross_pair_certificate_reuse'] is reuse
        if not reuse:
            assert e['gain'] in (0,1) and e['new_database_count'] in (0,1)
            assert e['source_query_evaluations'] in (0,2)
            assert 0<=e['order_probe_evaluations']<=2
    for e in validations:
        assert e['proposal_id'] in ended and ended[e['proposal_id']]['gain']>0
        assert ended[e['proposal_id']]['t']<=e['t']
        assert e['cross_pair_reuse'] is reuse
        if not reuse:
            assert e['query_count']==2 and len(set(e['target_pair']))==2
            assert set(by_id[e['proposal_id']]['target_pair'])==set(e['target_pair'])
    certificates=0
    for receipt in row['accepted_receipts']:
        p=journal/receipt['file'];assert p.name==receipt['file'] and sha(p)==receipt['sha256']
        event=json.loads(p.read_text())
        if event['kind']!='certificate':continue
        certificates+=1
        assert event['proposal_id'] in ended
        assert any(v['proposal_id']==event['proposal_id'] for v in validations)
        if not reuse:
            assert len(event['pairs'])==1 and event['pairs'][0]==event['target_pair']
            assert any(v['proposal_id']==event['proposal_id'] and set(v['target_pair'])==set(event['target_pair']) for v in validations)
    if not reuse:
        splits=[p for p in row['pairs'] if p['verdict']=='split']
        assert len(splits)==len(row['dbs'])==row['n_genuine_pairs']
        assert len({p['certificate'] for p in splits})==len(splits)
    return {'variant':variant,'cross_pair_certificate_reuse':reuse,
            'proposal_scopes_observed':len(proposals),'native_proposals_finished':len(finished),
            'validation_scopes_observed':len(validations),'certificate_events':certificates,
            'max_native_gain':max((e['gain'] for e in finished),default=0),
            'max_native_query_evaluations':max((e['source_query_evaluations'] for e in finished),default=0),
            'batch_candidate_generators_retained':True}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('output');a=ap.parse_args();out=Path(a.output)
    subprocess.run([sys.executable,str(ROOT/'scripts/check_leetcode_scale_unit.py'),str(out)],check=True)
    row=json.loads((out/'raw.jsonl').read_text());result=verify_witness_scope(row)
    runtime_path=Path(row['journal'])/'worker-runtime.json';runtime=json.loads(runtime_path.read_text())
    assert runtime==row['runtime_parent']
    assert runtime['python_hash_seed']==str(row['seed'])
    result.update({'raw_sha256':sha(out/'raw.jsonl'),'common_checked_sha256':sha(out/'checked.json'),
                   'worker_runtime_sha256':sha(runtime_path),'checker_sha256':sha(__file__)})
    data=json.dumps(result,sort_keys=True)+'\n';dest=out/'witness-checked.json'
    if dest.exists():assert dest.read_text()==data
    else:
        with dest.open('x') as stream:stream.write(data)
    print(data,end='')
if __name__=='__main__':main()
