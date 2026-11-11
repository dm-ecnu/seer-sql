"""One real official-worker functionality check per native API; no benchmark claim."""
import argparse,hashlib,json,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"eval"))
from seereval.schema import from_ddl
from seereval.multi_pair_shell import polygon_graph_multi,xdatan_graph_multi
from seereval import tools
p=argparse.ArgumentParser();p.add_argument("--tool",choices=("polygon","xdatan"),required=True);args=p.parse_args()
schema=from_ddl("native_spec","CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);",dialect="sqlite")
queries=[{"qid":"all","sql":"SELECT x FROM t"},{"qid":"empty","sql":"SELECT x FROM t WHERE 1=0"},{"qid":"one","sql":"SELECT x FROM t WHERE x=1"}]
worker=Path(tools.WORKERS)/tools.TOOLS[args.tool]["worker"]
function=polygon_graph_multi if args.tool=="polygon" else xdatan_graph_multi
result=function(schema,"sqlite",queries,budget_s=30,per_call_s=30,seed=0)
output={"scope":"single specification functionality check through original official worker; not a benchmark",
        "tool":args.tool,"queries":queries,"schema":schema.to_json(),"result":result,
        "tool_process_cpu_s":tools.cpu_used(),"incomplete_cpu_calls":tools.incomplete_cpu_calls(),
        "code_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        "source_sha256":{str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in [worker,ROOT/"eval/seereval/tools.py",ROOT/"eval/seereval/multi_pair_shell.py"]}}
out=ROOT/"runs/pair-state-check"/(args.tool+"-native-multi-bridge.json")
assert not out.exists();out.write_text(json.dumps(output,indent=2,default=str)+"\n")
print(json.dumps({"output":str(out),"sha256":hashlib.sha256(out.read_bytes()).hexdigest(),"native_calls":result["n_calls"],"genuine_pairs":result["n_genuine_pairs"],"native_verdicts":[r["native"]["verdict"] for r in result["calls"]],"tool_process_cpu_s":output["tool_process_cpu_s"],"incomplete_cpu_calls":output["incomplete_cpu_calls"]}),flush=True)
