"""Runtime-only ownership observations for the existing official checker."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import contextlib,json,os,re,sys,tempfile,time
from pathlib import Path

CONTAINER='seer-icde-sqldriller-verieql'
WORKSPACES=Path((SEER_ROOT + '/repro/official/call_workspaces'))

@contextlib.contextmanager
def observe_workspaces(call_id):
    from entry_metrics import record
    original=tempfile.TemporaryDirectory
    class Observed(original):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            path=Path(self.name)
            if path.parent==WORKSPACES and path.name.startswith('call-'):
                record('official_workspace_created',attempt_id=call_id,path=str(path))
    tempfile.TemporaryDirectory=Observed
    try:yield
    finally:tempfile.TemporaryDirectory=original

def install_native():
    from entry_metrics import record
    from docker.api.client import APIClient
    create,start=APIClient.exec_create,APIClient.exec_start
    owned=set()
    def observed_create(self,container,cmd,*args,**kwargs):
        target=self.inspect_container(CONTAINER)
        if container not in (CONTAINER,target['Id']):return create(self,container,cmd,*args,**kwargs)
        assert isinstance(cmd,list) and cmd[:2]==['python','/tmp/seer_icde_exec_meter.py'],'unverified official exec command'
        cpus=sorted(os.sched_getaffinity(0));assert len(cpus)==1
        allowed=set()
        for part in target['HostConfig']['CpusetCpus'].split(','):
            if '-' in part:
                a,b=map(int,part.split('-'));allowed.update(range(a,b+1))
            elif part:allowed.add(int(part))
        assert set(cpus)<=allowed,'host and native CPU masks differ'
        bounded=['taskset','-c',str(cpus[0]),'prlimit','--as=8589934592','--',*cmd]
        response=create(self,container,bounded,*args,**kwargs)
        identity=response['Id'];assert re.fullmatch('[0-9a-f]{64}',identity)
        record('official_docker_exec_created',exec_id=identity,container_id=target['Id'],cpu=cpus[0])
        owned.add(identity)
        return response
    def observed_start(self,exec_id,*args,**kwargs):
        if exec_id in owned:record('official_docker_exec_start_attempt',exec_id=exec_id)
        return start(self,exec_id,*args,**kwargs)
    APIClient.exec_create,APIClient.exec_start=observed_create,observed_start
    record('coverage',component='native',python_solver=False,source_queries=False,opaque_java=True,external_docker=True)

def ownership(entries,cgroup):
    events=entries['entries']
    starts={e['attempt_id']:e['pid'] for e in events if e['kind']=='generation_api_start' and e.get('tool')=='sqldriller_official'}
    workers={e['pid']:e['call_id'] for e in events if e['kind']=='native_worker_started' and e.get('tool')=='sqldriller_official' and e.get('call_id') in starts and '/sys/fs/cgroup'+e['cgroup']==cgroup['path']}
    executions=[e for e in events if e['kind']=='official_docker_exec_created' and workers.get(e['pid'])==e['call_id']]
    paths=[e['path'] for e in events if e['kind']=='official_workspace_created' and starts.get(e.get('attempt_id'))==e['pid']]
    return executions,paths

# Only the already registered meter's direct GNU timeout is signalled. GNU
# timeout retains ownership of its process group and its existing kill-after.
_CANCEL_TIMEOUT = r"""
import json,os,signal,sys
from pathlib import Path
root=int(sys.argv[1]); expected=sys.argv[2]
def stat(pid):return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(') ',1)[1].split()
assert root>1 and stat(root)[19]==expected,'meter PID identity changed'
argv=Path('/proc/'+str(root)+'/cmdline').read_bytes().split(b'\0')
assert argv[1]==b'/tmp/seer_icde_exec_meter.py','not the registered meter'
children=Path('/proc/'+str(root)+'/task/'+str(root)+'/children').read_text().split()
assert len(children)<=1,'unexpected meter children'
out={'action':'already_exited'}
for value in children:
    pid=int(value)
    try:fd=os.pidfd_open(pid)
    except ProcessLookupError:continue
    try:
        info=stat(pid)
        assert int(info[1])==root and int(info[2])==pid,'timeout ownership/group mismatch'
        cmd=Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\0')
        assert Path(os.fsdecode(cmd[0])).name=='timeout' and cmd[1:3]==[b'--signal=TERM',b'--kill-after=0.1s'],'unexpected timeout command'
        signal.pidfd_send_signal(fd,signal.SIGTERM)
        out={'action':'term_to_owned_timeout','timeout_pid':pid}
    except (FileNotFoundError,ProcessLookupError):pass
    finally:os.close(fd)
print(json.dumps(out))
"""

def cancel_owned_timeout(client,state):
    host_pid=state['Pid']; assert host_pid>1
    host=Path('/proc')/str(host_pid)
    ticks=host.joinpath('stat').read_text().rsplit(') ',1)[1].split()[19]
    ns=next(line.split()[1:] for line in host.joinpath('status').read_text().splitlines() if line.startswith('NSpid:'))
    assert len(ns)>=2 and int(ns[0])==host_pid,'cannot verify container PID namespace'
    current=client.api.exec_inspect(state['ID'])
    assert current['ContainerID']==state['ContainerID'] and current['Pid']==host_pid and current['Running']
    result=client.containers.get(state['ContainerID']).exec_run(['python','-c',_CANCEL_TIMEOUT,ns[-1],ticks])
    assert result.exit_code==0,'owned timeout cancellation failed: '+result.output.decode(errors='replace')[-500:]
    return json.loads(result.output)

def inspect_executions(executions):
    import docker
    client=docker.from_env(timeout=2)
    expected=client.containers.get(CONTAINER).id
    out=[]
    try:
        for event in executions:
            identity=event['exec_id'];assert re.fullmatch('[0-9a-f]{64}',identity)
            assert event['container_id']==expected
            state=client.api.exec_inspect(identity)
            assert state['ContainerID']==expected and state['ID']==identity
            cancellation=None
            if state['Running']:
                try:cancellation=cancel_owned_timeout(client,state)
                except (OSError,AssertionError) as exc:cancellation={'action':'failed','error':type(exc).__name__}
            until=time.monotonic()+3
            while True:
                state=client.api.exec_inspect(identity)
                assert state['ContainerID']==expected and state['ID']==identity
                if not state['Running'] or time.monotonic()>=until:break
                time.sleep(0.05)
            out.append({'kind':'docker_exec','exec_id':identity,'container_id':expected,'running':state['Running'],
                        'exit_code':state['ExitCode'],'cancellation':cancellation,'verified':state['Running'] is False})
    finally:client.close()
    return out

def cleanup(entries,cgroup,journal):
    import shutil,subprocess
    executions,paths=ownership(entries,cgroup);out=[]
    if executions:
        helper=(SEER_ROOT + '/sqldriller/venv-official/bin/python')
        try:
            result=subprocess.run([helper,str(Path(__file__).resolve()),'--inspect'],input=json.dumps(executions),
                                  capture_output=True,text=True,timeout=10,check=True)
            out=json.loads(result.stdout)
            assert len(out)==len(executions) and {e['exec_id'] for e in out}=={e['exec_id'] for e in executions}
        except (OSError,subprocess.SubprocessError,ValueError,AssertionError) as exc:
            out=[{'kind':'docker_inspection','verified':False,'error':type(exc).__name__}]
    for value in sorted(set(paths)):
        path=Path(value);assert path.parent==WORKSPACES and path.name.startswith('call-') and not path.is_symlink()
        record={'kind':'official_workspace','path':str(path),'verified':False}
        if all(e['verified'] for e in out):
            try:
                if path.exists():shutil.rmtree(path)
                record['verified']=not path.exists()
            except OSError as exc:record['error']=type(exc).__name__
        out.append(record)
    (Path(journal)/'official-cleanup.json').write_text(json.dumps(out,sort_keys=True)+'\n')
    return out

if __name__=='__main__':
    assert sys.argv[1:]==['--inspect']
    print(json.dumps(inspect_executions(json.load(sys.stdin)),sort_keys=True))
