"""Owned container lifecycle probe; no SQL/model evaluation."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import json,os,sys,time
from pathlib import Path
ROOT=Path((SEER_ROOT + '/sqld-deadline-wt'))
sys.path.insert(0,str(ROOT/'eval/tools'))
from entry_metrics import install_native
from execution_deadline import deadline_command
install_native({'_entry_metric_tool':'sqldriller_official','_entry_metric_call_id':sys.argv[1]})
import docker
client=docker.from_env(timeout=10)
try:
    command=deadline_command(['timeout','60s','python','-c','import time;time.sleep(60)'],float(sys.argv[2]))
    result=client.containers.get('seer-icde-sqldriller-verieql').exec_run(['python','/tmp/seer_icde_exec_meter.py',*command])
    print(json.dumps({'exit_code':result.exit_code}),flush=True)
finally:client.close()
