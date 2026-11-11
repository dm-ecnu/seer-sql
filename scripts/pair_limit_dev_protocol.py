"""Freeze and check the existing development inputs for native pair-graph runners."""
import argparse,hashlib,json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT.parent
OUT=BASE/'pair-limit-dev-matrix-v1'
ORIGINAL=BASE/'pair-dev-matrix-v1/protocol.json'
ORIGINAL_SHA='00ad96f5c677aa51eeda107e18ca75e72c80a376dc3790e8e9327b24f8dcf783'
DATASETS={'tpcds':8,'leetcode_batches':7,'cubes_d50':3,'cubes_d100':3}
METHODS={'pair_seer':157}
IDS=BASE/'gain-policy-wt/runs/gain-dev-v1/dev_ids'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def prepare():
 assert sha(ORIGINAL)==ORIGINAL_SHA
 original=json.loads(ORIGINAL.read_text())
 files=[ORIGINAL]; selected={}
 regression=ROOT/'runs/nondet-limit-regression-v2'
 required={
  'run.log':'c327d72e6b3cc9f34bcb6438b7bd8a941f53c9530f84d5d5b8fc5e136fcb9de7',
  'spec-before.jsonl':'8b96e82540cef01d8dfdb75d006d5fe63211258ea3f83c3e9a9b8e400490320a',
  'spec-after.jsonl':'901109e3ce916f1f3ccfeb77912f016484f48a804f2fd544c8a70cf39bcfa0e9'}
 assert (regression/'exit.code').read_text().strip()=='0'
 assert '244 passed in 268.20s' in (regression/'run.log').read_text()
 for name,digest in required.items():
  assert sha(regression/name)==digest
  files.append(regression/name)
 files += [regression/'exit.code',ROOT/'tests/test_nondeterministic_limit.py']
 for folder in ('src','eval'):
  for p in (ROOT/folder).rglob('*.py'):
   fixed=subprocess.check_output(['git','show','1bea5fec520f7236fc9752c24c40385865574fb3:'+str(p.relative_to(ROOT))],cwd=ROOT)
   assert p.read_bytes()==fixed,'core differs from validated revision: '+str(p)

 for ds,n in DATASETS.items():
  p=BASE/'datasets/batches'/f'{ds}.jsonl';i=IDS/f'{ds}.txt'
  ids=set(i.read_text().splitlines());assert len(ids)==n
  rows=[json.loads(s) for s in p.read_text().splitlines() if s]
  chosen={r['batch_id']:len(r['queries']) for r in rows if r['batch_id'] in ids}
  assert set(chosen)==ids;selected[ds]=chosen;files += [p,i]
  files += [(p.parent/r['schema_path']).resolve() for r in rows]
 for folder in ('src','eval','scripts'):
  files += list((ROOT/folder).rglob('*.py'))
 files += [ROOT/'scripts/run_pair_limit_dev_matrix.sh']
 for package in ('sqlwitness','parseval'):
  env='venv-sw' if package=='sqlwitness' else 'venv-pv'
  folder=BASE/env/'lib/python3.10/site-packages'/package
  assert folder.is_dir();files += list(folder.rglob('*.py'))
 jar=BASE/'xdatan/src/xdata/jars';assert (jar/'xdata.jar').is_file()
 files+=list(jar.glob('*.jar'))+[BASE/'xdatan/xd_run.py',BASE/'xdatan/src.commit']
 assert selected==original['selected']
 assert list(range(5))==original['seeds']
 config={'version':'pair-limit-dev-matrix-v1','original_protocol_sha256':ORIGINAL_SHA,
         'original_deadline_unix':ORIGINAL.stat().st_mtime+259200,
         'algorithm_revision':'1bea5fec520f7236fc9752c24c40385865574fb3','legal_tie_completions':True,'proof_scope_guard':True,
         'shared_pair_replay':True,'outcome_memo':False,'development_only':True,'selected':selected,'methods':METHODS,
         'seeds':list(range(5)),'batch_budget_s':600,'pair_call_s':60,'restart':True,
         'process_as_gib':8,'method_cgroup_memory_gib':10,'wall_limit_s':259200,
         'expected_records':105,'source_sha256':{str(p):sha(p) for p in sorted(set(files))}}
 OUT.mkdir(exist_ok=True)
 dest=OUT/'protocol.json'
 if dest.exists():assert json.loads(dest.read_text())==config,'source or protocol changed'
 else:dest.write_text(json.dumps(config,sort_keys=True,indent=2)+'\n')
 print(json.dumps({'prepared':True,'expected_records':105,'selected':selected,'protocol_sha256':sha(dest)},sort_keys=True))
def checked_config():
 p=OUT/'protocol.json';config=json.loads(p.read_text())
 assert config['source_sha256']=={path:sha(path) for path in config['source_sha256']},'frozen source changed'
 return config,sha(p)
def main():
 ap=argparse.ArgumentParser();ap.add_argument('mode',choices=['prepare','check','finish']);ap.add_argument('unit',nargs='?');a=ap.parse_args()
 if a.mode=='prepare':prepare();return
 config,stamp=checked_config()
 if a.mode=='check':print(stamp);return
 ds,method,seed=a.unit.split(':');seed=int(seed)
 assert ds in DATASETS and method in METHODS and seed in range(5)
 output=OUT/f'{ds}.{method}.s{seed}.jsonl'
 rows=[json.loads(s) for s in output.read_text().splitlines() if s]
 want=set(config['selected'][ds])
 assert len(rows)==len(want) and {r['batch_id'] for r in rows}==want
 for r in rows:
  assert r['seed']==seed and r['budget']==600 and r['tool']==method
  assert r['state_model']=='pair_evidence'
  contract=r['comparison_contract'];assert contract['mode']=='pair_evidence' and contract['budget_s']==600
  assert contract['tool']==method and contract['seed']==seed
  assert contract['shared_pair_replay'] is True and contract['outcome_memo'] is False
  assert all(sha(p)==h for p,h in contract['files'].items())
  if not r.get('runner_error'):
   n=config['selected'][ds][r['batch_id']]
   assert len(r['pairs'])==n*(n-1)//2
   assert len({frozenset((p['a'],p['b'])) for p in r['pairs']})==len(r['pairs'])
 marker={'unit':a.unit,'protocol_sha256':stamp,'output_sha256':sha(output),'records':len(rows),
         'runner_errors':sum(bool(r.get('runner_error')) for r in rows),
         'scope':'execution complete including failures; independent witness scoring still required'}
 dest=OUT/f'{ds}.{method}.s{seed}.done'
 if dest.exists():assert json.loads(dest.read_text())==marker,'completed source or output changed'
 else:dest.write_text(json.dumps(marker,sort_keys=True)+'\n')
 print(json.dumps(marker,sort_keys=True))
if __name__=='__main__':main()
