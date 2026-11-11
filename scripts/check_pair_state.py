"""Reproduce the retained comparison-contract counterexample; not a benchmark."""
import hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from seer.pair_state import PairSeer
from seer.core import replay
from seer2.schema import Schema
source=ROOT.parent/"repro/official/ordering-contract-gap.json"
old=json.loads(source.read_text())
results=[]
for policy in ("representatives","max_pairs"):
    schema=Schema("CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);","sqlite")
    seer=PairSeer(schema,old["queries"],dialect="sqlite",backend="sqlite",fast=False,
                  budget=5,levels=(1,2),max_k=2,symbolic_selection=policy)
    result=seer.run()
    assert seer.verdict("ascending","descending")["verdict"]=="split",result
    for q in ("ascending","descending"):
        assert seer.verdict("bridge",q)=={"verdict":"bounded_equivalent","k":2},result
    count=replay(schema,result,dialect="sqlite",backend="sqlite")
    assert count==1
    results.append({"policy":policy,"result":result,"fresh_replay_events":count})
output={"scope":"retained specification counterexample; no performance claim",
        "source":str(source),"source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
        "code_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        "tracked_source_clean":subprocess.run(["git","diff","--quiet","HEAD","--","src","scripts","tests"],cwd=ROOT).returncode==0,
        "source_sha256":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ("seer","seer2") for p in sorted((ROOT/"src"/folder).glob("*.py"))},
        "records":results}
path=ROOT/"runs/pair-state-check/ordering-candidate-lazy-import.json"
assert not path.exists(),"preserve previous attempts"
path.write_text(json.dumps(output,indent=2)+"\n")
print(json.dumps({"path":str(path),"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
                  "policies":[r["policy"] for r in results],"fresh_replay_events":[r["fresh_replay_events"] for r in results]}))
