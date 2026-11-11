import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'eval'),str(ROOT/'src')]
from hard_deadline import Collector,setup_worker,supervise,cgroup_snapshot
from pair_matrix_score import score_record
from seereval.schema import from_ddl


def spec_worker(batch,root,mode,budget,seed,deadline,conn,journal,mem_gb):
    setup_worker(conn,mem_gb)
    collector=Collector(batch,root,deadline,conn,journal)
    db={'t':[{'x':1,'payload':'ok'}]}
    if mode=='late':time.sleep(10)
    if mode=='cpuwork':
        until=time.process_time()+0.25
        while time.process_time()<until:pass
    if mode=='illegal':db={'t':[{'x':1,'payload':'a'},{'x':1,'payload':'b'}]}
    if mode=='fresh_changes':
        from seereval import validator
        original=validator.check_db;calls=0
        def actual_changed_replay(schema,dialect,instance,queries,*args,**kwargs):
            nonlocal calls
            calls+=1
            return original(schema,dialect,{} if calls==2 else instance,queries,*args,**kwargs)
        validator.check_db=actual_changed_replay
    if mode=='large':db['t'][0]['payload']='x'*600000
    if mode in ('native_group','unnotified_group'):
        launcher=subprocess.Popen.__mro__[1] if mode=='unnotified_group' else subprocess.Popen
        child=launcher([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
        (Path(journal)/'native-child.pid').write_text(str(child.pid))
    collector.offer(db)
    if mode=='error':
        conn.send({'kind':'worker_error','error':'injected after independently accepted certificate'})
        conn.close();return
    time.sleep(60)


@pytest.fixture
def batch(tmp_path):
    schema=from_ddl('deadline','CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL,payload TEXT);',dialect='sqlite')
    (tmp_path/'schema.json').write_text(json.dumps(schema.to_json()))
    return {'batch_id':'deadline-spec','dataset':'spec','dialect':'sqlite','schema_path':'schema.json',
            'queries':[{'qid':'all','sql':'SELECT x FROM t'},{'qid':'none','sql':'SELECT x FROM t WHERE 0'}]}


def run(tmp_path,batch,mode):
    before=time.monotonic()
    result=supervise(batch,str(tmp_path),mode,2,0,tmp_path/'journal',worker=spec_worker)
    assert time.monotonic()-before<5,'owned subprocess cleanup did not finish'
    assert all(r['received_s']<2 for r in result['accepted_receipts'])
    assert result['runner_error'] is None
    batch['_schema']=from_ddl('deadline','CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL,payload TEXT);',dialect='sqlite')
    outcomes,_=score_record(batch,result)
    return result,outcomes


def test_completed_certificate_survives_deadline_termination(tmp_path,batch):
    result,outcomes=run(tmp_path,batch,'partial')
    assert result['source_status']=='deadline'
    assert outcomes=={('all','none'):'split'}
    assert result['pairs'][0]['accepted_s']<2
    assert result['cpu_secs']>=0
    assert result['cpu_complete'] == (result['cpu_scope']=='exclusive_host_cgroup')
    saved=[json.loads(l) for l in (tmp_path/'journal/receipts.jsonl').read_text().splitlines()]
    assert saved==result['accepted_receipts']


@pytest.mark.parametrize('mode',['late','illegal','fresh_changes'])
def test_late_illegal_or_failed_fresh_replay_has_no_credit(tmp_path,batch,mode):
    result,outcomes=run(tmp_path,batch,mode)
    assert result['n_dbs']==0 and result['n_genuine_pairs']==0
    assert outcomes=={('all','none'):'undecided'}


def test_source_error_preserves_earlier_valid_evidence(tmp_path,batch):
    result,outcomes=run(tmp_path,batch,'error')
    assert result['source_status']=='error' and 'injected' in result['worker_error']
    assert outcomes=={('all','none'):'split'}


def test_large_database_payload_does_not_block_small_notification_pipe(tmp_path,batch):
    result,outcomes=run(tmp_path,batch,'large')
    assert outcomes=={('all','none'):'split'}
    assert len(result['dbs'][0]['t'][0]['payload'])==600000


def test_tool_in_new_session_is_terminated_with_its_owned_group(tmp_path,batch):
    result,outcomes=run(tmp_path,batch,'native_group')
    assert outcomes=={('all','none'):'split'}
    pid=int((tmp_path/'journal/native-child.pid').read_text())
    assert pid in result['owned_native_groups']
    stat=Path(f'/proc/{pid}/stat')
    assert not stat.exists() or stat.read_text().split(') ',1)[1].split()[0]=='Z'


def test_query_error_retains_all_edges_without_false_certificates(tmp_path,batch):
    batch['queries'].append({'qid':'bad','sql':'SELECT nonexistent FROM t'})
    result,outcomes=run(tmp_path,batch,'partial')
    assert result['error_class']==['bad']
    assert len(result['pairs'])==3
    assert outcomes=={('all','none'):'split',('all','bad'):'query_error',('none','bad'):'query_error'}


def test_exclusive_kernel_meter_covers_a_forced_worker_and_owned_tracker(tmp_path,batch):
    assert cgroup_snapshot()['exclusive_leaf'],'test requires its declared dedicated service'
    result,outcomes=run(tmp_path,batch,'cpuwork')
    assert outcomes=={('all','none'):'split'}
    assert result['source_status']=='deadline' and result['cpu_complete']
    assert result['cpu_scope']=='exclusive_host_cgroup' and result['cpu_secs']>=0.25
    assert result['cpu_meter']['after']['owned_resource_tracker_pids']
    evidence=os.environ.get('SEER_CPU_METER_EVIDENCE')
    if evidence:Path(evidence).write_text(json.dumps({k:result[k] for k in ('cpu_secs','cpu_complete','cpu_scope','cpu_meter','source_status','service_peak_memory_bytes','service_peak_memory_scope')},sort_keys=True)+'\n')


def test_unregistered_sibling_in_service_prevents_exclusive_claim():
    assert cgroup_snapshot()['exclusive_leaf']
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'])
    try:
        snapshot=cgroup_snapshot()
        assert child.pid in snapshot['pids'] and not snapshot['exclusive_leaf']
    finally:child.terminate();child.wait(timeout=2)


def test_unnotified_new_session_is_reclaimed_from_exclusive_service(tmp_path,batch):
    result,outcomes=run(tmp_path,batch,'unnotified_group')
    assert outcomes=={('all','none'):'split'}
    pid=int((tmp_path/'journal/native-child.pid').read_text())
    assert pid not in result['owned_native_groups']
    assert pid in result['service_cleanup']['killed_pids']
    assert result['service_cleanup']['verified']
    stat=Path(f'/proc/{pid}/stat')
    assert not stat.exists() or stat.read_text().split(') ',1)[1].split()[0]=='Z'
