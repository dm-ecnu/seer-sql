"""Server-only runtime check of an unchanged official earlier release; not a main-table run."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import json,pathlib,sqlite3,subprocess,time
from dataclasses import asdict
import os,resource
resource.setrlimit(resource.RLIMIT_AS,(8*1024**3,8*1024**3))
os.environ.setdefault("RAYON_NUM_THREADS","8")
from parseval import disprove
from parseval.generator import GenerationConfig
R=pathlib.Path((SEER_ROOT));O=R/"repro/parseval/release007-smoke";O.mkdir(exist_ok=True)
sha=subprocess.check_output(["git","-C",str(R/"parseval25/release007"),"rev-parse","HEAD"],text=True).strip()
assert sha.startswith("0d58127")
schema="CREATE TABLE t(id INTEGER PRIMARY KEY)"
q1="SELECT id FROM t WHERE id > 1"
for name,q2 in [("different","SELECT id FROM t WHERE id >= 1"),("identical",q1)]:
 db=O/(name+".sqlite");t=time.monotonic()
 result=disprove(q1,q2,schema,"sqlite:///"+str(db),"sqlite",generation_config=GenerationConfig(),timeout=15)
 record={"case":name,"upstream_commit":sha,"generation_config":asdict(GenerationConfig()),"wall_s":time.monotonic()-t,"verdict":result.verdict.value,"reason":result.error_msg,"generation":result.generation.to_dict()}
 if name=="different" and result.verdict.value.upper()=="NEQ":
  with sqlite3.connect(db) as c:a=c.execute(q1).fetchall();b=c.execute(q2).fetchall()
  record["replayed_results"]=[a,b];assert a!=b,record
 print(json.dumps(record,default=str),flush=True)
 assert result.verdict.value.upper()==("NEQ" if name=="different" else "EQ"),record
