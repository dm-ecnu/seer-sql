"""Optional B600 policy: retain the old per-decision cap in the frozen runner.

Only used if the author chooses fixed batch B600 instead of the C64 budgets.
This adapter changes budget resolution, not the search implementation.
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import importlib,json,sys
from pathlib import Path
R=Path((SEER_ROOT));W=R/'seer-c69-objective-code'
sys.path[:0]=[str(W/'src'),str(W/'eval'),str(W/'scripts')]

if __name__=='__main__':
    flag=sys.argv.index('--c69-panel');panel=sys.argv[flag+1];del sys.argv[flag:flag+2]
    flag=sys.argv.index('--c69-per-call-s');per_call=float(sys.argv[flag+1]);del sys.argv[flag:flag+2]
    assert panel in ('leetcode','xdatabm') and per_call==(60. if panel=='leetcode' else 120.)
    runner=importlib.import_module('run_stream_batch_sqlite' if panel=='leetcode' else 'run_stream_batch')
    original=runner.resolve_budget
    def resolve(args,batch):
        assert args.original_pair_timeout_s is None and args.budget==600.
        original(args,batch);args.per_call_s=per_call
    runner.resolve_budget=resolve
    out=Path(sys.argv[sys.argv.index('--out')+1]);before=runner.sha(__file__)
    runner.main()
    c=json.loads((out/'config.json').read_text());r=json.loads((out/'result.json').read_text())
    assert c['budget_s']==600. and c['per_call_s']==per_call and runner.sha(__file__)==before
    c.update(c69_budget_policy='fixed_batch_600_preserve_old_per_call',budget_adapter=str(Path(__file__).resolve()),budget_adapter_sha256=before)
    (out/'config.json').write_text(json.dumps(c,indent=2)+'\n')
    r['config_sha256']=runner.sha(out/'config.json')
    (out/'result.json').write_text(json.dumps(r,indent=2,default=str)+'\n')
