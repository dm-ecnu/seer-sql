"""Full Spider replay after a verified gateway outage; previous run stays intact."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import hashlib,importlib.util,json,random,re,subprocess,time,urllib.request
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path
R=Path((SEER_ROOT));O=R/'repro/official'
SOURCE=R/'seer-mx2-wt/scripts/repro_sqldriller_official.py'
spec=importlib.util.spec_from_file_location('native_reproduction',SOURCE)
native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
PAT=re.compile(r'connection (error|refused)|failed to (connect|establish)|no route|gateway unreachable',re.I)

def health():
 with urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness',timeout=5) as r:assert r.status==200

def job(row,logs):
 seed=int.from_bytes(hashlib.sha256(('0:'+row['batch_id']).encode()).digest()[:4],'big')
 random.seed(seed)
 try:
  health()
 except Exception as e:
  return {'batch_id':row['batch_id'],'benchmark':'spider','meta':row['meta'],'verdict':'ERROR','infra_transport_error':True,'error':'Gateway preflight failed: '+type(e).__name__,'recovery_seed':seed,'wall_s':0,'native_exception_logged':True}
 rec=native.case(('spider',row,R/'sqldriller/resources/database',logs))
 rec.update(recovery_seed=seed,recovery_run='spider-recovered-s0',supersedes_run='spider.jsonl',original_run_seed_recorded=False)
 text=(Path(logs)/(row['batch_id']+'.log')).read_text(errors='replace')
 rec['infra_transport_error']=bool(PAT.search(text))
 return rec

def main():
 health()
 secret=os.environ['LLM_API_KEY']
 payload=json.dumps({'model':'local-qwen','messages':[{'role':'user','content':'Reply OK.'}],'max_tokens':16,'temperature':0}).encode()
 req=urllib.request.Request('http://127.0.0.1:4000/v1/chat/completions',data=payload,headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=90) as response:probe=json.load(response)
 print(json.dumps({'gateway_probe':'passed','model':probe.get('model'),'has_text':bool(probe.get('choices',[{}])[0].get('message',{}).get('content'))}),flush=True)
 assert probe.get('choices',[{}])[0].get('message',{}).get('content'),'empty gateway probe'
 assert subprocess.check_output(['git','-C',str(native.UPSTREAM),'rev-parse','HEAD'],text=True).strip()==native.COMMIT
 out=O/'spider-recovered-s0.jsonl';logs=O/'spider-recovered-s0.logs';logs.mkdir(exist_ok=True)
 rows=[json.loads(l) for l in (R/'repro/sqldriller/datasets/sqld_spider_pairs.jsonl').read_text().splitlines()]
 done={r['batch_id'] for r in map(json.loads,out.read_text().splitlines())} if out.exists() else set()
 started=time.monotonic()
 with out.open('a') as f,ProcessPoolExecutor(2) as pool:
  futures=[pool.submit(job,row,str(logs)) for row in rows if row['batch_id'] not in done]
  for future in as_completed(futures):
   r=future.result();f.write(json.dumps(r)+'\n');f.flush()
 final=[json.loads(l) for l in out.read_text().splitlines()]
 assert len(final)==400 and len({r['batch_id'] for r in final})==400
 print(json.dumps({'rows':len(final),'infra_transport_errors':sum(r['infra_transport_error'] for r in final),'complete_without_transport_outage':not any(r['infra_transport_error'] for r in final),'this_invocation_wall_s':time.monotonic()-started}),flush=True)

if __name__=='__main__':main()
