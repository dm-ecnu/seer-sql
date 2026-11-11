"""Common online certificate boundary around the existing complete method runners.

The parent accepts only short Pipe notices received before its own deadline.
Payloads are immutable files published before notice; no late payload is promoted.
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse
import hashlib
import itertools
import json
import multiprocessing as mp
import os
from pathlib import Path
import resource
import signal
import sys
import time
import traceback
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'eval'),str(ROOT/'src')]
METHODS=('pair_seer','sqlwitness','parseval','xdatan','sqldriller_official')

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def cgroup_snapshot():
    """Read counters only from a leaf service containing our own ancestor chain.

    Session/user cgroups or concurrent unrelated work are never called exclusive.
    memory.peak remains a service-lifetime measurement on the deployed 6.8 kernel.
    """
    try:
        entry=next(l for l in Path('/proc/self/cgroup').read_text().splitlines() if l.startswith('0::'))
        path=Path('/sys/fs/cgroup')/entry.split('::',1)[1].lstrip('/')
        pids={int(p) for p in (path/'cgroup.procs').read_text().split()}
        lineage=set();pid=os.getpid()
        while pid>0 and pid not in lineage:
            lineage.add(pid)
            fields=Path(f'/proc/{pid}/stat').read_text().rsplit(') ',1)[1].split()
            pid=int(fields[1])
        from multiprocessing import resource_tracker
        tracker_pid=resource_tracker._resource_tracker._pid
        helpers=set()
        if tracker_pid:
            tracker=Path(f'/proc/{tracker_pid}/stat').read_text().rsplit(') ',1)[1].split()
            if int(tracker[1])==os.getpid():helpers.add(tracker_pid)
        stats={k:int(v) for k,v in (l.split() for l in (path/'cpu.stat').read_text().splitlines())}
        peak=path/'memory.peak'
        return {'path':str(path),'usage_usec':stats['usage_usec'],
                'exclusive_leaf':path.name.endswith('.service') and os.getpid() in pids and pids<=lineage|helpers and not any(p.is_dir() for p in path.iterdir()),
                'pids':sorted(pids),'ancestor_pids':sorted(lineage),'owned_resource_tracker_pids':sorted(helpers),
                'service_peak_memory_bytes':int(peak.read_text()) if peak.exists() else None}
    except (OSError,StopIteration,ValueError,KeyError):
        return {'exclusive_leaf':False,'reason':'unavailable_or_unverified_cgroup'}

def clear_owned_service(before):
    """Reclaim only new members of the service proved exclusive before launch.

    This closes the fork-to-Pipe-registration race of native separate sessions.
    pidfd pins each process identity before delivering the signal.
    """
    killed=set();until=time.monotonic()+1
    while True:
        current=cgroup_snapshot()
        if current.get('path')!=before['path']:
            return {'killed_pids':sorted(killed),'remaining_pids':None,'verified':False}
        safe=set(current['ancestor_pids'])|set(current['owned_resource_tracker_pids'])
        extra=set(current['pids'])-safe
        if not extra:return {'killed_pids':sorted(killed),'remaining_pids':[],'verified':True}
        if time.monotonic()>=until:
            return {'killed_pids':sorted(killed),'remaining_pids':sorted(extra),'verified':False}
        for pid in extra:
            fd=None
            try:
                fd=os.pidfd_open(pid)
                group=next(l for l in Path(f'/proc/{pid}/cgroup').read_text().splitlines() if l.startswith('0::'))
                actual=str(Path('/sys/fs/cgroup')/group.split('::',1)[1].lstrip('/'))
                if actual==before['path']:
                    signal.pidfd_send_signal(fd,signal.SIGKILL);killed.add(pid)
            except (OSError,StopIteration):pass
            finally:
                if fd is not None:os.close(fd)
        time.sleep(0.01)

def cleanup_xdatan(entries,cgroup,journal):
    """Use the existing psql endpoint only for this call's registered native worker DBs."""
    import subprocess
    starts={r['attempt_id'] for r in entries['entries'] if r['kind']=='generation_api_start' and r.get('tool')=='xdatan'}
    workers={int(r['pid']) for r in entries['entries'] if r['kind']=='native_worker_started' and r.get('tool')=='xdatan' and r.get('call_id') in starts and '/sys/fs/cgroup'+r.get('cgroup','')==cgroup['path']}
    out=[]
    psql=(SEER_ROOT + '/venv-pgsrv/lib/python3.10/site-packages/pgserver/pginstall/bin/psql')
    endpoint=[psql,'-h',os.environ.get('XD_ROOT',(SEER_ROOT + '/xdatan')),'-p',os.environ.get('XD_PORT','55930'),'-U','postgres','-qtA','postgres','-c']
    for pid in sorted(workers):
        assert pid>0
        name='xd_c'+str(pid)
        record={'worker_pid':pid,'database':name,'verified':False}
        try:
            subprocess.run(endpoint+[f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'],capture_output=True,text=True,timeout=8,check=True)
            remaining=subprocess.run(endpoint+["SELECT count(*) FROM pg_database WHERE datname='"+name+"'"],capture_output=True,text=True,timeout=8,check=True).stdout.strip()
            record['verified']=remaining=='0';record['remaining_databases']=int(remaining)
            scratch=Path(os.environ.get('XD_CALLS','/dev/shm/seer-xdatan/calls'))
            record['retained_native_scratch']=[str(p) for p in sorted(scratch.glob(str(pid)+'-*'))]
        except (OSError,subprocess.SubprocessError,ValueError) as exc:
            record['error']=str(exc)[:400]
        out.append(record)
    (Path(journal)/'xdatan-cleanup.json').write_text(json.dumps(out,sort_keys=True)+'\n')
    return out

class Collector:
    def __init__(self,batch,dataset_root,deadline,conn,journal,allow_cross_pair_reuse=True):
        from seereval.schema import Schema
        from seereval.validator import empty_db_errors
        self.batch,self.deadline,self.conn,self.journal=batch,deadline,conn,Path(journal)
        self.allow_cross_pair_reuse=bool(allow_cross_pair_reuse)
        self.schema=Schema.load(os.path.join(dataset_root,batch['schema_path']))
        self.sql={q['qid']:q['sql'] for q in batch['queries']}
        assert len(self.sql)==len(batch['queries'])
        self.seq=0;self.offers=0;self.checks=0;self.accepted=set()
        self.errors=empty_db_errors(self.schema,batch['dialect'],self.sql,self.left())
        self.emit({'kind':'errors','errors':self.errors})

    def left(self):return max(0.001,min(30,self.deadline-time.monotonic()))

    def emit(self,payload):
        if time.monotonic()>=self.deadline:return False
        name=f'event-{self.seq:06d}.json';self.seq+=1
        path=self.journal/name;data=json.dumps(payload,sort_keys=True).encode()
        with path.open('xb') as stream:stream.write(data)
        # Closing the immutable payload precedes the short notification.
        notice={'kind':'event','file':name,'sha256':hashlib.sha256(data).hexdigest()}
        if time.monotonic()>=self.deadline:return False
        self.conn.send(notice)
        return True

    def offer(self,instance,target_pair=None,proposal_id=None):
        from entry_metrics import observe_common_offer
        self._offer_observation_seq=getattr(self,'_offer_observation_seq',0)+1
        with observe_common_offer(self._offer_observation_seq,instance):
            return self._offer(instance,target_pair=target_pair,proposal_id=proposal_id)

    def _offer(self,instance,target_pair=None,proposal_id=None):
        from seer.core import _jsonable
        from seereval.validator import check_db,classify_from_check
        if time.monotonic()>=self.deadline:return
        if not self.allow_cross_pair_reuse and target_pair is None:
            raise ValueError('single-pair certification requires an explicit proposal target')
        if target_pair is not None:
            assert len(target_pair)==2 and len(set(target_pair))==2 and set(target_pair)<=set(self.sql)
            if any(q in self.errors for q in target_pair):return
        self.offers+=1
        sql={q:s for q,s in self.sql.items() if q not in self.errors and (target_pair is None or q in target_pair)}
        from entry_metrics import record
        record('witness_validation_scope',proposal_id=proposal_id,cross_pair_reuse=self.allow_cross_pair_reuse,
               query_count=len(sql),target_pair=list(target_pair) if target_pair is not None else None)
        primary=check_db(self.schema,self.batch['dialect'],instance,sql,self.left(),also_bare=False)
        self.checks+=1
        if primary['status']!='valid' or time.monotonic()>=self.deadline:return
        fresh=check_db(self.schema,self.batch['dialect'],instance,sql,self.left(),also_bare=False)
        self.checks+=1
        if fresh['status']!='valid' or time.monotonic()>=self.deadline:return
        # The database is immutable and shared by edge chunks, never copied into the Pipe.
        dbname=f'database-{self.offers:06d}.json';dbpath=self.journal/dbname
        dbdata=json.dumps(_jsonable(instance),sort_keys=True).encode()
        with dbpath.open('xb') as stream:stream.write(dbdata)
        dbsha=hashlib.sha256(dbdata).hexdigest();chunk=[]
        pair_targets=[tuple(target_pair)] if target_pair is not None else itertools.combinations(sql,2)
        for a,b in pair_targets:
            if time.monotonic()>=self.deadline:return
            key=frozenset((a,b))
            if key in self.accepted:continue
            if classify_from_check(primary,a,b)['class']=='genuine' and classify_from_check(fresh,a,b)['class']=='genuine':
                chunk.append([a,b])
            if len(chunk)==256:
                if not self.emit({'kind':'certificate','database':dbname,'database_sha256':dbsha,'pairs':chunk,'proposal_id':proposal_id,'target_pair':list(target_pair) if target_pair is not None else None}):return
                self.accepted.update(map(frozenset,chunk));chunk=[]
        if chunk and self.emit({'kind':'certificate','database':dbname,'database_sha256':dbsha,'pairs':chunk,'proposal_id':proposal_id,'target_pair':list(target_pair) if target_pair is not None else None}):
            self.accepted.update(map(frozenset,chunk))

def setup_worker(conn,mem_gb):
    os.setsid()
    conn.send({'kind':'started','pid':os.getpid()})
    # Existing tool adapters intentionally create separate process sessions.
    # Register those owned groups without changing their native timeout behavior.
    import subprocess
    native_popen=subprocess.Popen
    class TrackedPopen(native_popen):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            if kwargs.get('start_new_session'):
                conn.send({'kind':'native_group','pid':self.pid})
    subprocess.Popen=TrackedPopen
    if mem_gb:
        size=int(mem_gb*2**30);resource.setrlimit(resource.RLIMIT_AS,(size,size))

def initialize_duckdb_worker():
    """Release only the unused module-global database in this isolated worker.

    DuckDB 1.5 creates its default worker pool during import. The SEER sources
    use explicit connections with threads=1; the default pool is unused but can
    exhaust the service task limit before Arrow evaluates even three rows.
    """
    import duckdb
    before=len(list(Path('/proc/self/task').iterdir()))
    duckdb.default_connection().close()
    return {'duckdb_version':duckdb.__version__,'pid':os.getpid(),
            'threads_before_default_close':before,
            'threads_after_default_close':len(list(Path('/proc/self/task').iterdir())),
            'action':'closed unused module-global connection; explicit connections remain threads=1'}

ABLATIONS = {
    'full': {'share_query_encoding': True, 'encode_sigma': True},
    'no_deterministic_query_memo': {'share_query_encoding': False, 'encode_sigma': True},
    'no_hard_sigma': {'share_query_encoding': True, 'encode_sigma': False},
}


def ablation_config(method, name):
    if name in ('no_cross_pair_certificate_reuse','safe_goal_forest'):
        if method!='pair_seer':raise ValueError('pair-state ablations require pair_seer')
        return dict(ABLATIONS['full'])
    if name not in ABLATIONS:
        raise ValueError('unknown ablation: '+name)
    if method != 'pair_seer' and name != 'full':
        raise ValueError('encoding ablations require pair_seer')
    return dict(ABLATIONS[name])


def observe_encoding_ablation(journal, config):
    """Worker-local observations survive a hard cutoff; no outcome credit here."""
    from entry_metrics import record
    import seer.pair_state as pair_state
    original_level = pair_state.Level
    class ObservedLevel(original_level):
        def __init__(self, *args, **kwargs):
            record('encoding_level_start', k=args[4], **config)
            super().__init__(*args, **kwargs)
            record('encoding_level_built', k=self.k, encoded_queries=len(self.out),
                   encode_s=self.encode_s, encoding_stats=dict(self.enc.stats), **config)
        def find(self, classes, *args, **kwargs):
            record('encoding_target_request', k=self.k, groups=len(classes),
                   max_group_size=max(map(len,classes),default=0),
                   pair_goals=sum(len(c)*(len(c)-1)//2 for c in classes))
            started=time.perf_counter();result=None
            try:
                result=super().find(classes,*args,**kwargs)
                return result
            finally:
                record('symbolic_target_return',k=self.k,elapsed_s=time.perf_counter()-started,
                       status=result[0] if result is not None else 'exception',
                       separation_cache_pairs=len(self._sep),solver_check_starts=self.checks)
    original_goal_pairs=ObservedLevel.goal_pairs
    def goal_pairs(self,pairs,policy='all_pairs'):
        record('symbolic_goal_selection_start',k=self.k,policy=policy,input_pairs=len(pairs))
        selected,info=original_goal_pairs(self,pairs,policy)
        record('symbolic_goal_selection_built',k=self.k,**info)
        return selected,info
    ObservedLevel.goal_pairs=goal_pairs
    pair_state.Level = ObservedLevel
    original_reject = pair_state.PairSeer.record_sigma_rejection
    rejection_index = itertools.count()
    def observed_reject(self, db, k, violations):
        from seer.core import _jsonable
        path = Path(journal)/('sigma-rejection-%06d.json'%next(rejection_index))
        data = json.dumps({'k':k,'db':_jsonable(db),'violations':list(violations)},sort_keys=True).encode()
        with path.open('xb') as stream:
            stream.write(data)
        record('symbolic_sigma_rejected', k=k, database=path.name,
               sha256=hashlib.sha256(data).hexdigest(), violations_count=len(violations))
        return original_reject(self,db,k,violations)
    pair_state.PairSeer.record_sigma_rejection = observed_reject


def runtime_fingerprint():
    import importlib.metadata
    return {'python':sys.version,'python_hash_seed':os.environ.get('PYTHONHASHSEED'),
            'packages':{name:importlib.metadata.version(name) for name in ('z3-solver','sqlglot','duckdb','pyarrow')},
            'affinity':sorted(os.sched_getaffinity(0))}


def observe_pre_symbolic_state(cls, journal, config):
    from entry_metrics import record
    original=cls.symbolic_tier
    def observed(self):
        # This snapshot measures the actual prelude; the elapsed field is kept
        # outside its identity hash so paired runs can be compared exactly.
        state={'queries':list(self.queries.items()),'pending_pairs':self.pending_pairs(),
               'accepted_databases':self.dbs,'events':self.events}
        data=json.dumps(state,sort_keys=True).encode()
        path=Path(journal)/'pre-symbolic-state.json'
        with path.open('xb') as stream:stream.write(data)
        record('pre_symbolic_state', file=path.name, sha256=hashlib.sha256(data).hexdigest(),
               elapsed_s=self.elapsed(), pending_pairs=len(state['pending_pairs']),
               accepted_databases=len(self.dbs), **config)
        return original(self)
    cls.symbolic_tier=observed


def observe_witness_proposals(cls):
    from entry_metrics import record
    original=cls.witness_pairs
    def observed(self,pairs):
        selected=original(self,pairs)
        record('witness_proposal_scope',proposal_id=self.stats.get('witness_proposals',0),cross_pair_certificate_reuse=self.witness_reuse,
               pair_count=len(selected),target_pair=list(selected[0]) if not self.witness_reuse else None)
        return selected
    cls.witness_pairs=observed


def resolve_goal_policy(ablation,override=None):
    policy=override if override is not None else ('safe_forest' if ablation=='safe_goal_forest' else 'all_pairs')
    if policy not in ('all_pairs','safe_forest'):raise ValueError('unknown goal policy')
    return policy


def method_worker(batch,root,method,budget,seed,deadline,conn,journal,mem_gb,ablation="full",goal_policy=None):
    setup_worker(conn,mem_gb)
    os.chdir(journal)
    try:
        if method=='pair_seer' and batch['dialect'] in ('mysql','duckdb'):
            runtime=initialize_duckdb_worker()
            (Path(journal)/'duckdb-runtime.json').write_text(json.dumps(runtime,sort_keys=True)+'\n')
        os.environ['SEER_ENTRY_METRICS']=str(Path(journal)/'entries.jsonl')
        sys.path.insert(0,str(ROOT/'eval/tools'))
        from entry_metrics import install_host,instrument_generation
        install_host(os.environ['SEER_ENTRY_METRICS'],method)
        instrument_generation(deadline,journal)
        config=ablation_config(method,ablation)
        runtime=runtime_fingerprint()
        (Path(journal)/'worker-runtime.json').write_text(json.dumps(runtime,sort_keys=True)+'\n')
        if method=='pair_seer':observe_encoding_ablation(journal,config)
        collector=Collector(batch,root,deadline,conn,journal,allow_cross_pair_reuse=ablation!='no_cross_pair_certificate_reuse')
        if time.monotonic()>=deadline:return
        args=SimpleNamespace(budget=max(0.001,deadline-time.monotonic()),seed=seed,
                             symbolic_selection='representatives',outcome_memo=False,
                             share_query_encoding=config["share_query_encoding"],encode_sigma=config["encode_sigma"],
                             goal_policy=resolve_goal_policy(ablation,goal_policy),
                             witness_reuse=ablation!='no_cross_pair_certificate_reuse',
                             shared_pair_replay=True,comparison_state='pairs',tool=method,
                             per_call=min(60,budget),restart=True,
                             comparison_contract={'mode':'pair_evidence','online_supervisor':True})
        if method=='pair_seer':
            import run_pair_seer
            observe_pre_symbolic_state(run_pair_seer.PairSeer,journal,config)
            observe_witness_proposals(run_pair_seer.PairSeer)
            original=run_pair_seer.PairSeer.run_db
            def offer_after_native(self,db,origin):
                from entry_metrics import record
                before=len(self.dbs);executions=self.stats['executions'];probes=self.stats.get('executions_order_probe',0)
                proposal_id=self.stats.get('witness_proposals',0)
                gain=original(self,db,origin)
                if self.stats.get('witness_proposals',0)>proposal_id:
                    record('witness_proposal_finished',proposal_id=proposal_id,cross_pair_certificate_reuse=self.witness_reuse,
                           gain=gain,new_database_count=len(self.dbs)-before,
                           source_query_evaluations=self.stats['executions']-executions,
                           order_probe_evaluations=self.stats.get('executions_order_probe',0)-probes)
                from seer.core import from_jsonable
                for idx in range(before,len(self.dbs)):
                    instance=from_jsonable(self.dbs[idx])
                    if self.witness_reuse:
                        collector.offer(instance,proposal_id=proposal_id)
                    else:
                        # Exactly one pair event per native accepted database.
                        assert len(self.events)==len(self.dbs)
                        event_idx,pair,blocks=self.events[idx]
                        assert event_idx==idx and len(pair)==2
                        collector.offer(instance,target_pair=pair,proposal_id=proposal_id)
                return gain
            # The instrumentation exists only in this isolated worker process.
            run_pair_seer.PairSeer.run_db=offer_after_native
            result=run_pair_seer.run_batch(batch,args,root)
        else:
            import run_baseline
            from seereval.shell import _State
            original=_State.add_db
            def offer_after_native(self,instance,checked):
                original(self,instance,checked);collector.offer(instance)
            _State.add_db=offer_after_native
            result=run_baseline.run_batch(batch,args,root)
        summary={k:result.get(k) for k in ('runner_error','stop','stats','timing','n_calls','solver_calls','db_exec','cpu_complete','encoding_configuration','witness_configuration','goal_selection_configuration','goal_selection_trace')}
        collector.emit({'kind':'source_done','summary':summary,'offers':collector.offers,'common_checks':collector.checks})
    except BaseException:
        text=traceback.format_exc()
        (Path(journal)/'worker-error.txt').write_text(text)
        try:conn.send({'kind':'worker_error','error':text[-1500:]})
        except (BrokenPipeError,EOFError):pass
    finally:conn.close()

def supervise(batch,root,method,budget,seed,journal,mem_gb=8,worker=method_worker,ablation="full",goal_policy=None):
    resolved_goal=resolve_goal_policy(ablation,goal_policy)
    if method!='pair_seer' and resolved_goal!='all_pairs':raise ValueError('goal compression requires pair_seer')
    if worker is not method_worker and resolved_goal!='all_pairs':raise ValueError('custom worker does not implement goal compression')
    config=ablation_config(method,ablation)
    if ablation!="full" and worker is not method_worker:raise ValueError("custom workers do not implement encoding ablations")
    if budget<=0:raise ValueError('budget must be positive')
    journal=Path(journal);journal.mkdir(parents=True,exist_ok=False)
    receiver,sender=mp.get_context('spawn').Pipe(duplex=False)
    t0=time.monotonic();deadline=t0+budget
    before=resource.getrusage(resource.RUSAGE_CHILDREN)
    self_before=resource.getrusage(resource.RUSAGE_SELF);cg_before=cgroup_snapshot()
    if not cg_before.get('exclusive_leaf'):
        receiver.close();sender.close()
        raise RuntimeError('hard-deadline metered runs require an exclusive leaf systemd service')
    worker_kwargs={}
    if worker is method_worker:
        if ablation!='full':worker_kwargs['ablation']=ablation
        if goal_policy is not None:worker_kwargs['goal_policy']=goal_policy
    proc=mp.get_context('spawn').Process(target=worker,args=(batch,root,method,budget,seed,deadline,sender,str(journal),mem_gb),
                                      kwargs=worker_kwargs)
    receipts=[];worker_error=None;group_ready=False;source_done=False;native_groups=set()
    try:
        proc.start();sender.close()
        while time.monotonic()<deadline:
            if not receiver.poll(max(0,deadline-time.monotonic())):break
            try:notice=receiver.recv()
            except EOFError:break
            received=time.monotonic()
            if notice.get('kind')=='started':
                assert notice['pid']==proc.pid;group_ready=True
            if notice.get('kind')=='native_group':native_groups.add(notice['pid'])
            if received>=deadline:break
            if notice.get('kind')=='event':
                name=notice['file']
                assert Path(name).name==name and name.startswith('event-')
                receipts.append({**notice,'received_s':received-t0})
                with (journal/'receipts.jsonl').open('a') as receipt_log:
                    receipt_log.write(json.dumps(receipts[-1],sort_keys=True)+'\n');receipt_log.flush()
                # Source completion is a short final payload, already closed before notice.
                payload=json.loads((journal/name).read_text())
                if payload['kind']=='source_done':source_done=True;break
            elif notice.get('kind')=='worker_error':worker_error=notice['error'];break
        stopped=time.monotonic();cleanup0=stopped
    finally:
        # The receipt window is closed before any grace period or process cleanup.
        if proc.pid:
            if source_done:proc.join(timeout=0.1)
            if not group_ready:
                try:group_ready=os.getpgid(proc.pid)==proc.pid
                except ProcessLookupError:pass
            if group_ready:
                try:os.killpg(proc.pid,signal.SIGTERM)
                except ProcessLookupError:pass
            elif proc.is_alive():proc.terminate()
            # After closing the outcome window, consume only ownership notices.
            while receiver.poll(0):
                try:late=receiver.recv()
                except EOFError:break
                if late.get('kind')=='native_group':native_groups.add(late['pid'])
            for pid in native_groups:
                try:os.killpg(pid,signal.SIGTERM)
                except ProcessLookupError:pass
            proc.join(timeout=0.2)
            if group_ready:
                try:os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            for pid in native_groups:
                try:os.killpg(pid,signal.SIGKILL)
                except ProcessLookupError:pass
            if proc.is_alive():proc.kill()
            proc.join(timeout=1)
            if proc.is_alive():raise RuntimeError('owned worker did not terminate')
        service_cleanup=clear_owned_service(cg_before)
        receiver.close();sender.close()
    cleanup=time.monotonic()-cleanup0
    qids=[q['qid'] for q in batch['queries']];errors={};dbs=[];dbindex={};witness={};done=None
    for receipt in receipts:
        assert receipt['received_s']<budget
        path=journal/receipt['file'];assert digest(path)==receipt['sha256']
        event=json.loads(path.read_text())
        if event['kind']=='errors':errors=event['errors']
        elif event['kind']=='source_done':done=event
        elif event['kind']=='certificate':
            name=event['database'];assert Path(name).name==name
            assert digest(journal/name)==event['database_sha256']
            if name not in dbindex:
                dbindex[name]=len(dbs);dbs.append(json.loads((journal/name).read_text()))
            for a,b in event['pairs']:
                assert a in qids and b in qids and a!=b and a not in errors and b not in errors
                key=frozenset((a,b));assert key not in witness
                witness[key]=(dbindex[name],receipt['received_s'])
    pairs=[]
    for a,b in itertools.combinations(qids,2):
        accepted=witness.get(frozenset((a,b)))
        row={'a':a,'b':b,'certificate':accepted[0] if accepted else None,'verdict':'split' if accepted else 'undecided'}
        if accepted:row['accepted_s']=accepted[1]
        else:row['reason']='query_error' if a in errors or b in errors else 'deadline_or_no_witness'
        pairs.append(row)
    status='completed' if source_done else ('error' if worker_error else ('deadline' if stopped>=deadline else 'worker_exit'))
    if not service_cleanup['verified']:status='cleanup_failed'
    summary=(done or {}).get('summary',{})
    if summary.get('runner_error'):status='error';worker_error=summary['runner_error']
    usage=resource.getrusage(resource.RUSAGE_CHILDREN)
    self_after=resource.getrusage(resource.RUSAGE_SELF)
    fallback_cpu=usage.ru_utime+usage.ru_stime-before.ru_utime-before.ru_stime+self_after.ru_utime+self_after.ru_stime-self_before.ru_utime-self_before.ru_stime
    sys.path.insert(0,str(ROOT/'eval/tools'))
    from entry_metrics import read_entries
    entries=read_entries(journal/'entries.jsonl',t0,deadline)
    external_cleanup=cleanup_xdatan(entries,cg_before,journal) if method=='xdatan' else []
    if method=='sqldriller_official':
        from official_lifecycle import cleanup as official_cleanup
        external_cleanup=official_cleanup(entries,cg_before,journal)
    if any(not r['verified'] for r in external_cleanup):status='external_cleanup_failed'
    parent_runtime=runtime_fingerprint()
    cg_after=cgroup_snapshot()
    cg_ok=cg_before.get('exclusive_leaf') and cg_after.get('exclusive_leaf') and cg_before.get('path')==cg_after.get('path') and cg_after['usage_usec']>=cg_before['usage_usec']
    cpu=(cg_after['usage_usec']-cg_before['usage_usec'])/1e6 if cg_ok else fallback_cpu
    complete=(bool(cg_ok) or (status=='completed' and proc.exitcode==0)) and batch['dialect']=='sqlite' and method not in ('xdatan','sqldriller_official')
    return {'batch_id':batch['batch_id'],'dataset':batch['dataset'],'tool':method,'seed':seed,'budget':budget,
            'runtime_parent':parent_runtime,'ablation':ablation,
            'goal_policy':resolved_goal if method=='pair_seer' else None,
            'witness_reuse':ablation!='no_cross_pair_certificate_reuse' if method=='pair_seer' else None,'encoding_configuration':config if method=='pair_seer' else None,
            'state_model':'pair_evidence','pairs':pairs,'dbs':dbs,'n_dbs':len(dbs),'n_genuine_pairs':len(witness),
            'error_class':sorted(errors),'error_msgs':errors,'runner_error':None,'worker_error':worker_error,
            'source_status':status,'source_exitcode':proc.exitcode,'source_summary':done,'owned_native_groups':sorted(native_groups),'service_cleanup':service_cleanup,
            'journal':str(journal.resolve()),'accepted_receipts':receipts,'last_accept_s':max((r['accepted_s'] for r in pairs if r['verdict']=='split'),default=None),
            'acceptance_window_s':min(stopped-t0,budget),'cleanup_s':cleanup,'wall_secs':time.monotonic()-t0,
            'cpu_secs':cpu,'cpu_complete':complete,'cpu_scope':'exclusive_host_cgroup' if cg_ok else 'supervisor_and_reaped_worker_tree',
            'cpu_meter':{'before':cg_before,'after':cg_after},'cpu_rusage_lower_bound':fallback_cpu,
            'service_peak_memory_bytes':cg_after.get('service_peak_memory_bytes') if cg_ok else None,
            'service_peak_memory_scope':'entire owning service lifetime, including file cache; not a per-batch peak',
            'n_calls':entries['counts'].get('generation_api_start',0) if method!='pair_seer' else None,
            'call_metrics':entries,'external_backend_cleanup':external_cleanup,'cpu_host_complete':bool(cg_ok),
            'peak_memory_bytes':None,
            'cost_limitations':['external DBMS/Docker CPU excluded','CPU through result graph aggregation; final output JSON write excluded','per-batch peak memory remains unavailable']}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--dataset',required=True);ap.add_argument('--only',required=True)
    ap.add_argument('--method',choices=METHODS,required=True);ap.add_argument('--budget',type=float,required=True)
    ap.add_argument('--seed',type=int,default=0);ap.add_argument('--out',required=True);ap.add_argument('--journal',required=True)
    ap.add_argument('--ablation',choices=tuple(ABLATIONS)+('no_cross_pair_certificate_reuse','safe_goal_forest'),default='full')
    ap.add_argument('--goal-policy',choices=('all_pairs','safe_forest'))
    a=ap.parse_args();ablation_config(a.method,a.ablation);resolved_goal=resolve_goal_policy(a.ablation,a.goal_policy)
    if a.method!='pair_seer' and resolved_goal!='all_pairs':raise ValueError('goal compression requires pair_seer')
    root=str(Path(a.dataset).resolve().parent)
    batches=[json.loads(l) for l in Path(a.dataset).read_text().splitlines()]
    wanted=set(Path(a.only).read_text().splitlines());selected=[b for b in batches if b['batch_id'] in wanted]
    assert len(selected)==len(wanted),'missing or duplicate requested batch'
    files=[Path(__file__),Path(a.dataset),Path(a.only)]
    files += [p for folder in ('scripts','src','eval') for p in (ROOT/folder).rglob('*.py')]
    files += list({Path(root,b['schema_path']).resolve() for b in batches})
    from seer.goal_graph import dependency_path
    files.append(dependency_path())
    contract={'mode':'pair_evidence','budget_s':a.budget,'tool':a.method,'seed':a.seed,'hard_parent_receipt_deadline':True,
              'fresh_common_dbms_runs':2,'ablation':a.ablation,
              'goal_policy':resolved_goal if a.method=='pair_seer' else None,
              'witness_reuse':a.ablation!='no_cross_pair_certificate_reuse' if a.method=='pair_seer' else None,
              'encoding_configuration':ablation_config(a.method,a.ablation) if a.method=='pair_seer' else None,'files':{str(p.resolve()):digest(p) for p in sorted(set(files))}}
    out=Path(a.out)
    if out.exists():raise FileExistsError('new cohort never overwrites or resumes an unverified output')
    with out.open('x') as stream:
        for i,batch in enumerate(selected):
            row=supervise(batch,root,a.method,a.budget,a.seed,Path(a.journal)/str(i),ablation=a.ablation,goal_policy=a.goal_policy)
            row['comparison_contract']=contract
            stream.write(json.dumps(row,sort_keys=True)+'\n');stream.flush()
            print(json.dumps({k:row[k] for k in ('batch_id','tool','source_status','n_genuine_pairs','n_dbs','last_accept_s','wall_secs','cpu_complete')}),flush=True)
            if not row['service_cleanup']['verified'] or any(not r['verified'] for r in row['external_backend_cleanup']):raise RuntimeError('owned service cleanup incomplete; retained accepted record and stopped')

if __name__=='__main__':main()
