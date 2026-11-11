"""Whole-graph native replay and actual symbolic-target population binding."""
import argparse,hashlib,json,math,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from seer.goal_graph import dependency_path,WHEEL_SHA256

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def verify_goal_trace(row):
    policy=row['goal_policy'];assert policy in ('all_pairs','safe_forest')
    contract=row['comparison_contract'];assert contract['goal_policy']==policy
    assert row['encoding_configuration']==contract['encoding_configuration']
    assert row['witness_reuse']==contract['witness_reuse']
    assert contract['files'][str(dependency_path())]==WHEEL_SHA256
    pending=None;built=[];requests=0;returns=[]
    for e in row['call_metrics']['entries']:
        if e['kind']=='symbolic_goal_selection_built':
            assert e['policy']==policy and pending is None
            assert e['input_pairs']>0 and 0<e['selected_pairs']<=e['input_pairs']
            assert e['omitted_pairs']==e['input_pairs']-e['selected_pairs']
            assert math.isfinite(e['selection_s']) and e['selection_s']>=0
            if policy=='safe_forest':
                assert e['eligibility_evaluated'] and e['dependency_sha256']==WHEEL_SHA256
                assert e['eligible_pairs']+e['protected_pairs']==e['input_pairs']
                assert e['selected_pairs']>=e['protected_pairs']
                assert e['forest_edges']==e['eligible_vertices']-e['eligible_components']
                assert e['forest_edges']+e['protected_pairs']==e['selected_pairs']
                assert sum(e['protection_reasons'].values())==e['protected_pairs']
            else:
                assert not e['eligibility_evaluated'] and e['selected_pairs']==e['input_pairs']
            pending=e;built.append(e)
        elif e['kind']=='encoding_target_request':
            assert pending is not None and e['k']==pending['k']
            assert e['max_group_size']==2
            assert e['groups']==e['pair_goals']==pending['selected_pairs']
            pending=None;requests+=1
        elif e['kind']=='symbolic_target_return':returns.append(e)
    return {'policy':policy,'target_requests':requests,'selection_rounds_completed':len(built),
            'input_pairs_over_completed_rounds':sum(e['input_pairs'] for e in built),
            'selected_pairs_over_completed_rounds':sum(e['selected_pairs'] for e in built),
            'omitted_pairs_over_completed_rounds':sum(e['omitted_pairs'] for e in built),
            'selection_seconds_completed_lower_bound':sum(e['selection_s'] for e in built),
            'target_returns_observed':len(returns),
            'target_seconds_completed_lower_bound':sum(e['elapsed_s'] for e in returns)}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('output');a=ap.parse_args();out=Path(a.output)
    subprocess.run([sys.executable,str(ROOT/'scripts/check_leetcode_scale_unit.py'),str(out)],check=True)
    row=json.loads((out/'raw.jsonl').read_text());result=verify_goal_trace(row)
    runtime_path=Path(row['journal'])/'worker-runtime.json';runtime=json.loads(runtime_path.read_text())
    assert runtime==row['runtime_parent'] and runtime['python_hash_seed']==str(row['seed'])
    result.update({'raw_sha256':sha(out/'raw.jsonl'),'common_checked_sha256':sha(out/'checked.json'),
                   'worker_runtime_sha256':sha(runtime_path),'checker_sha256':sha(__file__)})
    data=json.dumps(result,sort_keys=True)+'\n';dest=out/'goal-checked.json'
    if dest.exists():assert dest.read_text()==data
    else:
        with dest.open('x') as stream:stream.write(data)
    print(data,end='')
if __name__=='__main__':main()
