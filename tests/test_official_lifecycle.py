import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import json,os,subprocess,sys,tempfile,time
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'eval'),str(ROOT/'src'),str(ROOT/'eval/tools')]
from hard_deadline import Collector,setup_worker,supervise
from official_lifecycle import WORKSPACES,ownership
from seereval.schema import from_ddl
from pair_matrix_score import score_record

def docker_worker(batch,root,method,budget,seed,deadline,conn,journal,mem_gb):
    setup_worker(conn,mem_gb)
    from entry_metrics import install_host,record
    from official_lifecycle import observe_workspaces
    path=Path(journal)/'entries.jsonl'
    install_host(str(path),method)
    collector=Collector(batch,root,deadline,conn,journal)
    collector.offer({'t':[{'x':1}]})
    call_id=str(os.getpid())+'-probe'
    record('generation_api_start',tool=method,attempt_id=call_id,n_queries=2)
    with observe_workspaces(call_id):
        with tempfile.TemporaryDirectory(prefix='call-',dir=WORKSPACES) as workspace:
            (Path(workspace)/'owned.txt').write_text('owned lifecycle test')
            env=dict(os.environ,SEER_ENTRY_METRICS=str(path))
            probe_log=(Path(journal)/'native-probe.log').open('w')
            child=subprocess.Popen([(SEER_ROOT + '/sqldriller/venv-official/bin/python'),
                  str(ROOT/'tests/official_exec_probe.py'),call_id,str(time.monotonic()+30)],env=env,
                  stdout=probe_log,stderr=subprocess.STDOUT,start_new_session=True)
            probe_log.close()
            until=time.monotonic()+10
            while time.monotonic()<until:
                if path.exists() and 'official_docker_exec_start_attempt' in path.read_text():
                    saved=[json.loads(line) for line in path.read_text().splitlines() if line.endswith('}')]
                    identity=next(e['exec_id'] for e in saved if e['kind']=='official_docker_exec_created')
                    running=subprocess.check_output([(SEER_ROOT + '/sqldriller/venv-official/bin/python'),
                        '-c','import docker,sys; print(docker.from_env(timeout=2).api.exec_inspect(sys.argv[1])["Running"])',identity],text=True,timeout=4).strip()
                    if running=='True':
                        record('docker_running_confirmed',exec_id=identity)
                        conn.send({'kind':'worker_error','error':'intentional host interruption after Docker start'})
                        time.sleep(60)
                if child.poll() is not None:raise AssertionError('native probe exited before interruption')
                time.sleep(0.01)
            raise AssertionError('Docker start was not observed')

def test_real_docker_ends_after_host_interruption_and_only_owned_workspace_is_removed(tmp_path):
    schema=from_ddl('official-lifecycle','CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);',dialect='sqlite')
    (tmp_path/'schema.json').write_text(json.dumps(schema.to_json()))
    batch={'batch_id':'official-lifecycle','dataset':'spec','dialect':'sqlite','schema_path':'schema.json',
           'queries':[{'qid':'all','sql':'SELECT x FROM t'},{'qid':'none','sql':'SELECT x FROM t WHERE 0'}]}
    WORKSPACES.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='unrelated-spec-',dir=WORKSPACES) as unrelated:
        start=time.monotonic()
        row=supervise(batch,str(tmp_path),'sqldriller_official',14,0,tmp_path/'journal',worker=docker_worker)
        assert time.monotonic()-start<18
        assert row['source_status']=='error' and 'intentional host interruption' in row['worker_error']
        assert row['cpu_host_complete'] and not row['cpu_complete']
        assert Path(unrelated).exists(),'unrelated workspace was removed'
        clean=row['external_backend_cleanup']
        assert any(r['kind']=='docker_exec' for r in clean)
        assert any(r['kind']=='official_workspace' for r in clean)
        assert all(r['verified'] for r in clean)
        assert any(e['kind']=='docker_running_confirmed' for e in row['call_metrics']['entries'])
        assert all(r['exit_code'] in (137,143) for r in clean if r['kind']=='docker_exec')
        assert all(r['cancellation']['action']=='term_to_owned_timeout' for r in clean if r['kind']=='docker_exec')
        assert all(not r['running'] for r in clean if r['kind']=='docker_exec')
        assert all(not Path(r['path']).exists() for r in clean if r['kind']=='official_workspace')
        batch['_schema']=schema
        assert score_record(batch,row)[0]=={('all','none'):'split'}

def test_unmatched_native_cgroup_or_call_id_does_not_grant_cleanup_ownership():
    entries={'entries':[{'kind':'generation_api_start','tool':'sqldriller_official','attempt_id':'own','pid':10},
        {'kind':'native_worker_started','tool':'sqldriller_official','call_id':'other','pid':11,'cgroup':'/wrong.service'},
        {'kind':'official_docker_exec_created','pid':11,'call_id':'other','exec_id':'a'*64},
        {'kind':'official_workspace_created','pid':12,'attempt_id':'own','path':'/tmp/unowned'}]}
    assert ownership(entries,{'path':'/sys/fs/cgroup/own.service'})==([],[])
