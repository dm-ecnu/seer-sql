"""Reuse the common frozen runner with the official-release adapter recorded."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import json,sys,time
from pathlib import Path
R=Path((SEER_ROOT))
W=R/'seer-c69-objective-code'
sys.path[:0]=[str(W/'src'),str(W/'eval'),str(W/'scripts')]
import run_stream_batch as runner
from bird_parseval_adapter import run

if __name__=='__main__':
    assert sys.argv[sys.argv.index('--method')+1]=='parseval'
    out=Path(sys.argv[sys.argv.index('--out')+1])
    adapter_files=[Path(__file__),Path(__file__).with_name('bird_parseval_adapter.py'),
        Path(__file__).with_name('parseval007_pool_worker.py'),
        R/'parseval007-repro-wt/eval/tools/parseval007_snapshot.py',
        R/'parseval25/release007/scripts/exp_sqlite_disprover.py',
        R/'repro/parseval/release007-environment.freeze']
    hashes={str(p):runner.sha(p) for p in adapter_files}
    runner.run_sqlwitness=run
    runner.main()
    config=json.loads((out/'config.json').read_text())
    result=json.loads((out/'result.json').read_text())
    assert all(runner.sha(p)==h for p,h in hashes.items())
    config.update(native_mode='official_release007_bird_pair_function_defaults',
        official_upstream_commit='0d58127c5d6df74caca170b80c80f312b95cc238',
        native_pair_timeout_s=360,native_generation_seed=12322,adapter_files=hashes,
        randomness_contract='arrival permutation only; common seed pool seed 0; native official generation seed 12322')
    (out/'config.json').write_text(json.dumps(config,indent=2)+'\n')
    result['config_sha256']=runner.sha(out/'config.json')
    (out/'result.json').write_text(json.dumps(result,indent=2,default=str)+'\n')
