"""Freeze the predeclared complete nested scaling cohort and certify each unit."""
import argparse,hashlib,json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT.parent
DATA=BASE/'datasets/batches/leetcode-scale-v1'
OUT=BASE/'leetcode-scale-v3'
METHODS={'pair_seer':168,'sqlwitness':169,'parseval':170,'xdatan':171}
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def check():
    protocol=json.loads((OUT/'protocol.json').read_text())
    assert all(sha(p)==h for p,h in protocol['files'].items()),'frozen source/input changed'
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()==protocol['code_commit']
    return protocol
def main():
    ap=argparse.ArgumentParser();ap.add_argument('mode',choices=('prepare','check','finish','complete'));ap.add_argument('method',nargs='?');ap.add_argument('bid',nargs='?');ap.add_argument('seed',nargs='?',type=int);a=ap.parse_args()
    if a.mode=='prepare':
        inputs=json.loads((DATA/'input.json').read_text())
        files=[p for f in ('scripts','eval','src') for p in (ROOT/f).rglob('*.py')]
        files+=list((ROOT/'scripts').glob('*leetcode_scale*.sh'))
        files+=list(DATA.glob('*.ids'))+[DATA/'input.json',DATA/'leetcode_scale.jsonl',Path(inputs['source_file'])]
        files += [Path(p) for p in inputs['schemas']]
        previous=json.loads((BASE/'pair-dev-matrix-v1/protocol.json').read_text())
        native={p:h for p,h in previous['source_sha256'].items() if '/site-packages/' in p or '/xdatan/' in p}
        assert native and all(sha(p)==h for p,h in native.items())
        hashes={str(p.resolve()):sha(p) for p in sorted(set(files))};hashes.update(native)
        protocol={'version':'leetcode-scale-v3','code_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'methods':METHODS,'sizes':inputs['sizes'],'seeds':list(range(5)),'budget_s':60,'pair_call_max_s':60,
            'process_as_gib':8,'unit_cgroup_gib':10,'tasks_max':128,'unit_failure_wall_s':240,
            'expected_test_units':2160,'test_ids':(DATA/'test.ids').read_text().splitlines(),
            'pilot_dev_id':'leetcode-1084-n500','source_input_sha256':sha(DATA/'input.json'),
            'cpu_scope':'exclusive host service; external MySQL/PostgreSQL CPU missing',
            'memory_scope':'single-batch service lifetime including startup/graph/page cache, excluding external DBMS',
            'score_scope':'all distinct SQL pairs; complete workload; 18 test parents shared at all six sizes',
            'files':hashes}
        assert len(protocol['test_ids'])==108
        OUT.mkdir(exist_ok=True);data=json.dumps(protocol,sort_keys=True,indent=2)+'\n';p=OUT/'protocol.json'
        if p.exists():assert p.read_text()==data
        else:
            with p.open('x') as stream:stream.write(data)
    protocol=check()
    if a.mode in ('prepare','check'):
        print(json.dumps({'protocol_sha256':sha(OUT/'protocol.json'),'code_commit':protocol['code_commit'],'expected_test_units':2160}));return
    if a.mode=='finish':
        assert a.method in METHODS and a.bid in protocol['test_ids'] and a.seed in protocol['seeds']
        path=OUT/a.method/('s'+str(a.seed))/a.bid
        checked=json.loads((path/'checked.json').read_text())
        assert checked['method']==a.method and checked['seed']==a.seed and checked['batch_id']==a.bid and checked['split']=='test'
        assert checked['raw_sha256']==sha(path/'raw.jsonl')
        marker={'protocol_sha256':sha(OUT/'protocol.json'),'checked_sha256':sha(path/'checked.json'),'raw_sha256':sha(path/'raw.jsonl')}
        data=json.dumps(marker,sort_keys=True)+'\n';dest=path/'done.json'
        if dest.exists():assert dest.read_text()==data
        else:
            with dest.open('x') as stream:stream.write(data)
        print(json.dumps({'finished':str(path),'source_status':checked['source_status']}));return
    missing=[]
    for method in METHODS:
        for seed in protocol['seeds']:
            for bid in protocol['test_ids']:
                path=OUT/method/('s'+str(seed))/bid
                if not (path/'done.json').is_file():missing.append(str(path))
                else:
                    marker=json.loads((path/'done.json').read_text())
                    assert marker['protocol_sha256']==sha(OUT/'protocol.json')
                    assert marker['raw_sha256']==sha(path/'raw.jsonl') and marker['checked_sha256']==sha(path/'checked.json')
    print(json.dumps({'expected':2160,'complete_units':2160-len(missing),'missing_units':len(missing),'missing_first':missing[:4]}))
    if missing:raise SystemExit(2)
if __name__=='__main__':main()
