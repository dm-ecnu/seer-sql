"""Run only repaired BIRD scoring; preserve failed postprocess history."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import csv,json,subprocess,sys,time
from pathlib import Path
from receipts import read,write,sha
ROOT=Path((SEER_ROOT + '/repro/c69-production-v1'))
HERE=Path(__file__).resolve().parent

def main():
 original={str(p):sha(p)for p in (ROOT/'bird/result.json',ROOT/'bird/config.json')}
 subprocess.run([sys.executable,str(HERE/'postprocess.py'),str(ROOT),'bird_table'],check=True)
 marker=read(ROOT/'tables/table-birdpool-c69.ready.json')
 assert marker['rows']==3
 assert all(sha(ROOT/'tables'/('table-birdpool-c69.'+ext))==h for ext,h in marker['files'].items())
 assert all(sha(p)==h for p,h in original.items())
 repair=dict(returncode=0,finished=time.time(),summary_sha256=marker['files']['json'],
  previous_terminal=read(ROOT/'post-terminal/bird_table.json'),log=str(ROOT/'logs/bird_table-repair-v3.log'),
  unchanged_source_hashes=original,scoring_sha256=sha(HERE/'score_bird_c69.py'))
 write(ROOT/'post-terminal/bird_table-repair-v3.json',repair)
 for name in ('completion.json','status.json'):
  data=read(ROOT/name);data['postprocess']['bird_table']=0
  data.setdefault('postprocess_repairs',{})['bird_table']=str(ROOT/'post-terminal/bird_table-repair-v3.json')
  write(ROOT/name,data)
 print(json.dumps(dict(event='BIRD_table_ready',rows=3,files=marker['files'],source_receipts_unchanged=True)),flush=True)
if __name__=='__main__':main()
