"""Finish and expose each validated table without changing source receipts."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import csv,json,os,subprocess,sys
from pathlib import Path
from receipts import read,write,sha
R=Path((SEER_ROOT));P=R/'repro';PY=R/'venv-seer/bin/python';HERE=Path(__file__).resolve().parent

def ready(root,name,stage,rows):
    out=root/'tables';data=read(out/(name+'.json'))
    with (out/(name+'.csv')).open() as h:actual=len(list(csv.DictReader(h)))
    assert actual==rows,(name,actual,rows)
    # Keep original attempts visible even when infrastructure replay succeeded.
    attempts={s:{k:read(root/s/'result.json').get(k) for k in ('attempted_units','failed_attempts','infrastructure_retry_groups')} for s in stage}
    data['attempt_accounting']=attempts
    write(out/(name+'.json'),data)
    aux={'table-xdatabm-c69':'fig6b-xdatabm-c69.csv','table-birdpool-c69':'birdpool-c69-size-points.csv'}.get(name)
    auxiliary={aux:sha(out/aux)} if aux and (out/aux).exists() else {}
    write(out/(name+'.ready.json'),dict(auxiliary_files=auxiliary,name=name,rows=actual,source_commit=read(root/'config.json')['source_commit'],files={suffix:sha(out/(name+'.'+suffix))for suffix in ('csv','json')},attempt_accounting=attempts))

def call(name,*args):subprocess.run([str(PY),str(HERE/name),*map(str,args)],check=True)

def main(root,kind):
    out=root/'tables';out.mkdir(exist_ok=True)
    if kind=='replay':
        # This process coordinates independently leased replay workers. The
        # outer idle-core wrapper must not restrict every child to its own core.
        # Linux intersects this mask with the enclosing cpuset; each worker
        # subsequently obtains its original c63 physical-core lease.
        os.sched_setaffinity(0,range(os.cpu_count()))
        call('replay_saved.py',root/'formal',root/'replay');return
    if kind=='xdata_table':
        call('summarize_xdata.py','--formal',root/'formal','--replay',root/'replay/result.json','--out',out)
        ready(root,'table-xdatabm-c69',['formal'],1)
    elif kind=='formal_tables':
        call('summarize_formal.py','--formal',root/'formal','--ablation',root/'ablation',
             '--legacy-csv',HERE/'reference-table3-leetcode-sqlite-m36.csv','--replay',root/'replay/result.json','--out',out)
        ready(root,'table3-leetcode-c69',['formal'],280)
        ready(root,'table-ablation-xdatabm-c69',['ablation'],6)
    else:
        assert kind=='bird_table'
        call('score_bird_c69.py',root/'bird',out)
        ready(root,'table-birdpool-c69',['bird'],3)

if __name__=='__main__':main(Path(sys.argv[1]),sys.argv[2])
