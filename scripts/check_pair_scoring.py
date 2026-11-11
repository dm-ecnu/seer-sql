"""Real-engine counterexamples to inferring pair differences from exact signatures."""
import hashlib,itertools,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"eval"))
from seereval.schema import from_ddl
from seereval.validator import check_db,classify_from_check,empty_db_errors
from seereval.shell import _State,_refine
from seereval.pair_evidence import pair_evidence
schema=from_ddl("pair_contract","CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);",dialect="sqlite")
db={"t":[{"x":1},{"x":2}]}
cases={
 "mixed_order":{"bridge":"SELECT x FROM t","ascending":"SELECT x FROM t ORDER BY x ASC","descending":"SELECT x FROM t ORDER BY x DESC"},
 "instance_error":{"all":"SELECT x FROM t","none":"SELECT x FROM t WHERE 0","error":"SELECT CASE WHEN COUNT(*) > 1 THEN ABS(-9223372036854775808) ELSE 0 END FROM t"}
}
rows=[]
for name,queries in cases.items():
 errors=empty_db_errors(schema,"sqlite",queries)
 assert not errors,errors
 checked=check_db(schema,"sqlite",db,queries)
 assert checked["status"]=="valid",checked
 state=_State(schema,"sqlite",queries,list(queries));state.add_db(db,checked)
 partition=_refine([list(queries)],state)
 group={q:i for i,qs in enumerate(partition) for q in qs}
 pairs=[]
 for a,b in itertools.combinations(queries,2):
  outcome=classify_from_check(checked,a,b)
  pairs.append({"a":a,"b":b,"legacy_partition_split":group[a]!=group[b],"common_pair_class":outcome["class"]})
 assert sum(p["legacy_partition_split"] for p in pairs)==3
 assert sum(p["common_pair_class"]=="genuine" for p in pairs)==1
 evidence=pair_evidence(itertools.combinations(queries,2),[(0,checked)])
 assert evidence["n_genuine_pairs"]==1
 rows.append({"pair_evidence":evidence,"case":name,"queries":queries,"database":db,"checked":checked,"legacy_partition":partition,"pairs":pairs})
output={"scope":"common scoring contract diagnostic; frozen runs and metrics unchanged","code_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),"source_sha256":{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in ("eval/seereval/shell.py","eval/seereval/validator.py","eval/seereval/engine.py","eval/seereval/pair_evidence.py")},"records":rows}
p=ROOT/"runs/pair-state-check/common-scoring-evidence.json";assert not p.exists();p.write_text(json.dumps(output,indent=2)+"\n")
print(json.dumps({"source":str(p),"sha256":hashlib.sha256(p.read_bytes()).hexdigest(),"cases":[{"case":r["case"],"legacy_splits":sum(p["legacy_partition_split"] for p in r["pairs"]),"genuine":sum(p["common_pair_class"]=="genuine" for p in r["pairs"]),"other_pair_classes":[p["common_pair_class"] for p in r["pairs"] if p["common_pair_class"]!="genuine"]} for r in rows]}))
