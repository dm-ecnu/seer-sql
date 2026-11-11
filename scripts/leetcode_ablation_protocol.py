"""Complete paired encoding ablation cohort, using the existing scale checker."""
import argparse,hashlib,json,subprocess
from pathlib import Path
import leetcode_scale_protocol as base
from hard_deadline import ABLATIONS,runtime_fingerprint
ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT.parent/'datasets/batches/leetcode-scale-v1'
OUT=ROOT.parent/'leetcode-ablation-scale-v1'
VERSION='leetcode-ablation-scale-v1'
VARIANTS=tuple(ABLATIONS)
sha=base.sha

def check():
    base.OUT=OUT
    return base.check()

def verify_configuration(row,variant,seed,expected_runtime=None):
    assert variant in VARIANTS and seed in range(5)
    expected=ABLATIONS[variant]
    assert row['tool']=='pair_seer' and row['ablation']==variant
    assert row['encoding_configuration']==expected
    c=row['comparison_contract']
    assert c['ablation']==variant and c['encoding_configuration']==expected
    runtime=json.loads((Path(row['journal'])/'worker-runtime.json').read_text())
    assert runtime['python_hash_seed']==str(seed)==row['runtime_parent']['python_hash_seed']
    assert runtime['packages']==row['runtime_parent']['packages']
    assert runtime['python']==row['runtime_parent']['python']
    if expected_runtime is not None:
        assert {k:runtime[k] for k in ('python','packages')}==expected_runtime
    assert len(runtime['affinity'])==1 and runtime['affinity']==row['runtime_parent']['affinity']
    pre=[e for e in row['call_metrics']['entries'] if e['kind']=='pre_symbolic_state']
    assert len(pre)<=1
    for e in pre:
        p=Path(row['journal'])/e['file'];assert p.name==e['file'] and sha(p)==e['sha256']
        state=json.loads(p.read_text())
        assert len(state['pending_pairs'])==e['pending_pairs']
        assert len(state['accepted_databases'])==e['accepted_databases']
    for e in row['call_metrics']['entries']:
        if e['kind']=='symbolic_sigma_rejected':
            p=Path(row['journal'])/e['database'];assert p.name==e['database'] and sha(p)==e['sha256']
            assert len(json.loads(p.read_text())['violations'])==e['violations_count']
    return runtime,pre

def finish(variant,bid,seed,output=None,test=True,protocol=None):
    protocol=check() if protocol is None else protocol
    assert variant in protocol['methods'] and seed in protocol['seeds']
    assert bid in protocol['test_ids'] if test else bid==protocol['pilot_dev_id']
    path=Path(output) if output else OUT/variant/('s'+str(seed))/bid
    checked=json.loads((path/'checked.json').read_text())
    row=json.loads((path/'raw.jsonl').read_text())
    assert checked['method']=='pair_seer' and checked['seed']==seed and checked['batch_id']==bid
    assert checked['split']==('test' if test else 'dev')
    assert checked['raw_sha256']==sha(path/'raw.jsonl')
    runtime,pre=verify_configuration(row,variant,seed,protocol['runtime'])
    if test:
        lane=(VARIANTS.index(variant)-seed-protocol['test_ids'].index(bid))%3
        assert runtime['affinity']==[184+lane]
    assert row['service_cleanup']['verified'] and row['source_status'] in ('completed','deadline')
    assert not row.get('worker_error') and not row.get('runner_error')
    assert all(protocol['files'].get(p)==h for p,h in row['comparison_contract']['files'].items())
    marker={'protocol_sha256':sha(OUT/'protocol.json'),'checked_sha256':sha(path/'checked.json'),
            'raw_sha256':sha(path/'raw.jsonl'),'worker_runtime_sha256':sha(Path(row['journal'])/'worker-runtime.json')}
    data=json.dumps(marker,sort_keys=True)+'\n';dest=path/'done.json'
    if dest.exists():assert dest.read_text()==data
    else:
        with dest.open('x') as stream:stream.write(data)
    return marker

def prepare():
    inputs=json.loads((DATA/'input.json').read_text())
    files=[p for f in ('scripts','eval','src') for p in (ROOT/f).rglob('*.py')]
    files+=list((ROOT/'scripts').glob('*leetcode_ablation*.sh'))
    files+=list((ROOT/'scripts/systemd').glob('seer-lcablation-score-v1.*'))
    files+=list(DATA.glob('*.ids'))+[DATA/'input.json',DATA/'leetcode_scale.jsonl',Path(inputs['source_file'])]
    files += [Path(p) for p in inputs['schemas']]
    ids=(DATA/'test.ids').read_text().splitlines()
    assert len(ids)==108 and len(set(ids))==108
    protocol={'version':VERSION,'code_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'methods':{v:ABLATIONS[v] for v in VARIANTS},'sizes':inputs['sizes'],'seeds':list(range(5)),
              'budget_s':60,'process_as_gib':8,'unit_cgroup_gib':10,'tasks_max':128,'unit_failure_wall_s':240,
              'queue_cpu_affinities':[184,185,186],'configuration_assignment':'variant[(lane+seed+batch_index)%3]',
              'runtime':{k:runtime_fingerprint()[k] for k in ('python','packages')},
              'python_hash_seed':'equals repeat seed, set before parent and child Python startup',
              'expected_test_units':1620,'test_ids':ids,'pilot_dev_id':'leetcode-1084-n500',
              'source_input_sha256':sha(DATA/'input.json'),
              'score_scope':'same 18 test parents at six nested sizes; pool per seed then five-seed mean and sample SD',
              'cost_scope':'host service; external MySQL CPU and total method memory missing',
              'prelude_scope':'pre-symbolic full state retained; stage attribution requires equal state identity',
              'failure_policy':'retain raw accepted certificates; stop failed lane; no implicit rerun or deletion',
              'files':{str(p.resolve()):sha(p) for p in sorted(set(files))}}
    OUT.mkdir(exist_ok=True);data=json.dumps(protocol,sort_keys=True,indent=2)+'\n';dest=OUT/'protocol.json'
    if dest.exists():assert dest.read_text()==data
    else:
        with dest.open('x') as stream:stream.write(data)
    return check()

def complete():
    protocol=check();missing=[];count=0
    for v in protocol['methods']:
        for seed in protocol['seeds']:
            for bid in protocol['test_ids']:
                path=OUT/v/('s'+str(seed))/bid
                if not (path/'done.json').is_file():missing.append(str(path));continue
                assert finish(v,bid,seed,protocol=protocol)==json.loads((path/'done.json').read_text());count+=1
    print(json.dumps({'expected':1620,'complete_units':count,'missing_units':len(missing),'missing_first':missing[:4]}))
    if missing:raise SystemExit(2)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('mode',choices=('prepare','check','finish','pilot','complete'))
    ap.add_argument('variant',nargs='?');ap.add_argument('bid',nargs='?');ap.add_argument('seed',nargs='?',type=int)
    ap.add_argument('--output');a=ap.parse_args()
    if a.mode=='prepare':prepare()
    if a.mode=='complete':complete();return
    if a.mode in ('finish','pilot'):
        print(json.dumps(finish(a.variant,a.bid,a.seed,a.output,test=a.mode=='finish')));return
    p=check();print(json.dumps({'protocol_sha256':sha(OUT/'protocol.json'),'expected_test_units':p['expected_test_units']}))
if __name__=='__main__':main()
