"""Run the released native MySQL driver; only isolate DB names and observe completed records."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse,fcntl,hashlib,importlib.util,json,os,resource,subprocess,sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse,urlunparse
resource.setrlimit(resource.RLIMIT_AS,(8*1024**3,8*1024**3))
os.environ.setdefault("RAYON_NUM_THREADS","8")
os.environ.setdefault("OPENBLAS_NUM_THREADS","1")
R=Path((SEER_ROOT));UP=R/"parseval25/release007"
SRC=UP/"scripts/exp_mysql_disprover.py"
spec=importlib.util.spec_from_file_location("native_release_driver",SRC)
native=importlib.util.module_from_spec(spec);sys.modules[spec.name]=native;spec.loader.exec_module(native)
ORIGINAL=native._process_disprove_case
OUT=None

def observed(*args,**kwargs):
 record=ORIGINAL(*args,**kwargs)
 if OUT:
  observed_record=dict(record)
  from sqlalchemy.engine import make_url
  password=make_url(os.environ["SQLWITNESS_MYSQL_URL"]).password
  if password and observed_record.get("error_msg"):
   observed_record["error_msg"]=observed_record["error_msg"].replace(password,"[REDACTED]")
  text=json.dumps(observed_record,default=str)
  with (OUT/"completed.jsonl").open("a") as f:
   fcntl.flock(f,fcntl.LOCK_EX);f.write(text+"\n");f.flush()
 return record

if __name__=="__main__":
 parser=argparse.ArgumentParser();parser.add_argument("--out",required=True,type=Path);parser.add_argument("--limit",type=int);parser.add_argument("--workers",type=int,default=4);a=parser.parse_args()
 OUT=a.out.resolve();OUT.relative_to(R/"repro/parseval");OUT.mkdir(parents=True,exist_ok=True)
 assert not (OUT/"completed.jsonl").exists(),"preserve prior output; select a distinct run directory"
 sha=subprocess.check_output(["git","-C",str(UP),"rev-parse","HEAD"],text=True).strip();assert sha=="0d58127c5d6df74caca170b80c80f312b95cc238"
 subprocess.run(["git","-C",str(UP),"diff","--exit-code","HEAD","--","src","scripts/exp_mysql_disprover.py"],check=True)
 data=UP/"data/mysql/leetcode.jsonlines"
 from parseval.generator import GenerationConfig
 print(json.dumps({"upstream_commit":sha,"native_driver_sha256":hashlib.sha256(SRC.read_bytes()).hexdigest(),"data_sha256":hashlib.sha256(data.read_bytes()).hexdigest(),"generation_defaults":asdict(GenerationConfig()),"native_query_task_timeout_s":60,"workers":a.workers,"worker_as_gib":8,"limit":a.limit,"scope":"native_release_diagnosis; existing main version unchanged"}),flush=True)
 namespace="seer_icde_pv007_"+("pilot" if a.limit else "full")+"_"
 def isolated_url(base,index):
  return urlunparse(urlparse(base)._replace(path="/"+namespace+str(index)))
 native._make_task_connection_string=isolated_url
 native._process_disprove_case=observed
 native.run_disprove_experiment(str(data),connection_string=os.environ["SQLWITNESS_MYSQL_URL"],output_dir=str(OUT),limit=a.limit,workers=a.workers,timeout=60)
