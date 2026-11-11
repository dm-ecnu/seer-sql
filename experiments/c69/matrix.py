"""Immutable C69 matrices; all methods/variants in a group share one core lease."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse,concurrent.futures as cf,hashlib,json,os,subprocess,sys,time
from pathlib import Path
R=Path((SEER_ROOT));W=R/'seer-c69-objective-code';P=R/'repro'
sys.path.insert(0,str(W/'scripts'))
from c63_consistency import read,write,sha,PY
from c64_pipeline import environment
from c65_ablation import VARIANTS,variant_order

def available():
    return int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')))*1024

def freeze():
    f=read(P/'c69-objective-v2/freeze.json')
    assert read(f['gate'])['passed'] and sha(f['gate'])==f['gate_sha256']
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=W,text=True).strip()
    assert head==f['source_commit']
    assert subprocess.check_output(['git','rev-parse',f['tag']],cwd=W,text=True).strip()==head
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=W,text=True).strip()
    return f

def groups(stage,policy):
    if stage=='bird':
        from score_bird_public_original_paired_v1 import input_rows,sample_rows,DATA
        source,test,dev=input_rows();sample_rows(test)
        assert sha(DATA/'test.ids')=='c17d7e80bc8df43b74fab0f14b6ac4c4023bc6600452af521aca28583e1e3886'
        assert sha(DATA/'dev.ids')=='0bce15b902b0bd73c68c0102ebebd60e98ef84d82c7d79a4d7c1f78ea71b48d8'
        input_sha=sha(DATA/'bird_public.jsonl')
        raw=R/'datasets/raw/public-batch-v1'
        published=[e for e in read(raw/'source-files.json') if e['dest'].startswith('spotit/')]
        assert {(e['repo'],e['ref']) for e in published}=={
            ('atremante26/VeriEQL','fb1bdaae8f3e4a544d08feffa44bc14270d99329')}
        for e in published:
            content=(raw/e['dest']).read_bytes()
            assert hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()==e['sha']
        assert sha(raw/'bird-published-pools.jsonl')=='b601d39d1641a2b50ab4415bf223ea4807b152032dcb3bf6b6c4add48c8f8eb2'
        methods=['seer_stream_sqlwitness','sqlwitness','parseval'];out=[]
        # Put the predeclared efficiency sample first; include every other
        # frozen test/dev batch afterwards without changing any denominator.
        sampled=[r['batch'] for r in sample_rows(test)]
        ids=sampled+[b for b in test+dev if b not in set(sampled)]
        for i,bid in enumerate(ids):
            for seed in range(5):
                order=methods[(i+seed)%3:]+methods[:(i+seed)%3]
                if (i+seed)%2:order=order[::-1]
                out.append(dict(panel='bird',batch=bid,seed=seed,input=str(DATA/'bird_public.jsonl'),
                    input_sha256=input_sha,order=order,split='test' if bid in test else 'dev',hard_bytes=8*1024**3))
        assert len(out)==1534*5
        return out
    assert policy in ('c64','600'),'LC/XData budget policy must be explicitly confirmed'
    source=read(P/'c64-class-v5/formal/config.json')['jobs'];assert len(source)==700
    if stage=='formal':return [dict(j,order=['full'],hard_bytes=6*1024**3) for j in source]
    assert stage=='ablation'
    selected=[j for j in source if j['panel']=='xdatabm'];assert len(selected)==420
    return [dict(j,order=variant_order(i//5,j['seed']),hard_bytes=6*1024**3) for i,j in enumerate(selected)]

def group_attempt(root,index,attempt=0):
    plan=read(root/'config.json');g=plan['groups'][index]
    assert freeze()['source_commit']==plan['source_commit']
    assert all(sha(p)==h for p,h in plan['adapter_files'].items()), 'controller or adapters changed after matrix start'
    assert sha(g['input'])==g['input_sha256']
    if 'config' in g:assert sha(g['config'])==g['old_config_sha256']
    core,=os.sched_getaffinity(0);records=[];env=environment()
    base=root if attempt==0 else root/'infrastructure-retry'/f'g{index}-a{attempt}'
    for name in ('logs','terminal','groups'):(base/name).mkdir(parents=True,exist_ok=True)
    env.update(PYTHONDONTWRITEBYTECODE='1',RAYON_NUM_THREADS='1',ARROW_DEFAULT_MEMORY_POOL='system')
    for variant in g['order']:
        isbird=plan['stage']=='bird'
        old=read(g['config']) if not isbird else None
        budget=600 if isbird or plan['budget_policy']=='600' else old['budget_s']
        method=variant if isbird else 'seer_stream_sqlwitness'
        out=base/'runs'/g['batch']/f's{g["seed"]}'/variant
        out.parent.mkdir(parents=True,exist_ok=True)
        assert not out.exists(),'retain every immutable prior attempt'
        script=(Path(__file__).with_name('run_bird_parseval.py') if method=='parseval' else
            W/'scripts'/('run_stream_batch_sqlite.py' if g['panel']=='leetcode' else 'run_stream_batch.py'))
        fixed=not isbird and plan['budget_policy']=='600'
        if fixed:script=Path(__file__).with_name('run_fixed_budget.py')
        args=[str(PY),str(script),'--dataset',g['input'],'--batch',g['batch'],'--method',method,
            '--seed',str(g['seed']),'--order','shuffle' if isbird else old['order'],
            '--schedule','class_bounded','--quick-schedule','compare','--out',str(out)]
        if isbird or plan['budget_policy']=='600':args+=['--budget','600']
        else:args+=['--original-pair-timeout-s',str(old['per_call_s'])]
        if fixed:args+=['--c69-panel',g['panel'],'--c69-per-call-s',str(old['per_call_s'])]
        if not isbird:args+=VARIANTS[variant]
        log=base/'logs'/f'{index}-{variant}.log';rss=base/'logs'/f'{index}-{variant}-rss.txt'
        started=time.time()
        cg=next(x.split('::',1)[1] for x in Path('/proc/self/cgroup').read_text().splitlines() if x.startswith('0::'))
        events=Path('/sys/fs/cgroup')/cg.lstrip('/')/'memory.events'
        oom_before=int(dict(x.split() for x in events.read_text().splitlines()).get('oom_kill',0))
        with log.open('x') as h:
            rc=subprocess.call(['prlimit',f'--as={g["hard_bytes"]}','--',
                '/usr/bin/time','-v','-o',str(rss),'timeout','--signal=TERM','--kill-after=5s',str(budget+240),*args],
                cwd=W,env=env,stdout=h,stderr=subprocess.STDOUT)
        record=dict(batch=g['batch'],panel=g['panel'],seed=g['seed'],variant=variant,core=core,
            started=started,finished=time.time(),returncode=rc,path=str(out),status='failed',budget_s=budget,attempt=attempt,
            oom_kill_delta=int(dict(x.split() for x in events.read_text().splitlines()).get('oom_kill',0))-oom_before)
        if rss.exists():record['peak_process_rss_bytes']=next((int(l.split(':',1)[1])*1024 for l in rss.read_text().splitlines() if 'Maximum resident set size (kbytes)' in l),None)
        if rc==0:
            try:
                c=read(out/'config.json');r=read(out/'result.json')
                assert c['source_commit']==plan['source_commit'] and c['affinity']==[core]
                assert c['budget_s']==budget and c['seed']==g['seed']
                if not isbird:assert c['per_call_s']==old['per_call_s']
                assert r['config_sha256']==sha(out/'config.json')
                assert r['metrics']['processed_queries']+r['metrics']['unprocessed_queries']==len(c['query_ids'])
                assert r['metrics']['invalid_admitted_certificates']==0
                record.update(status='complete',wall_s=r['metrics']['wall_s'],
                    censored=bool(r['metrics']['censored_at_budget']),result_sha256=sha(out/'result.json'))
            except Exception as exc:record.update(status='invalid_result',error=repr(exc))
        if record['status']!='complete':
            diagnostic='\n'.join(p.read_text(errors='replace')[-12000:] for p in (log,out/'stderr.log') if p.exists())
            method_fault=record['oom_kill_delta']>0 or rc==124 or any(t in diagnostic for t in ('AssertionError','MemoryError','std::bad_alloc','out of memory'))
            infrastructure=not method_fault and (rc in (137,143,-9,-15) or any(t in diagnostic for t in ('Input/output error','Connection refused','Connection reset','server closed the connection','could not connect to server','Network is unreachable')))
            record['failure_kind']='infrastructure' if infrastructure else 'method_or_unclassified'
        records.append(record);write(base/'terminal'/f'{index}-{variant}.json',record)
    assert len(records)==len(g['order']) and {x['core'] for x in records}=={core}
    assert all(a['finished']<=b['started'] for a,b in zip(records,records[1:]))
    result=dict(job=g,records=records,core=core)
    write(base/'groups'/f'{index}.json',result)
    return result


def group(root,index):
    history=[]
    for attempt in range(2):
        result=group_attempt(root,index,attempt)
        write(root/'attempts'/f'{index}-a{attempt}.json',result)
        history.append(result)
        if not any(r.get('failure_kind')=='infrastructure' for r in result['records']) or any(r.get('failure_kind')=='method_or_unclassified' for r in result['records']):break
    for r in result['records']:write(root/'terminal'/f'{index}-{r["variant"]}.json',r)
    result['attempt_history']=[str(root/'attempts'/f'{index}-a{i}.json') for i in range(len(history))]
    write(root/'groups'/f'{index}.json',result)


def run(root,stage,policy):
    frozen=freeze();jobs=groups(stage,policy)
    root.mkdir(parents=True,exist_ok=False)
    for name in ('logs','terminal','groups'):(root/name).mkdir()
    adapter_files=[Path(__file__),Path(__file__).with_name('score_bird_public_original_paired_v1.py')]
    if stage=='bird':adapter_files += [Path(__file__).with_name(n+'.py') for n in ('run_bird_parseval','bird_parseval_adapter','parseval007_pool_worker')]
    elif policy=='600':adapter_files.append(Path(__file__).with_name('run_fixed_budget.py'))
    plan=dict(stage=stage,budget_policy=policy,source_commit=frozen['source_commit'],groups=jobs,
        adapter_files={str(p.resolve()):sha(p) for p in adapter_files},
        units=sum(len(g['order']) for g in jobs),controller_sha256=sha(__file__),
        pairing='adjacent methods/variants within a shared physical-core lease',
        admission='sum active worker hard limits <= current MemAvailable / 2')
    write(root/'config.json',plan);active={};cursor=0;finished=[]
    def launch(i):
        g=jobs[i];command=['systemd-run','--user','--scope','--quiet',f'--unit={root.name}-g{i}',
            '-p',f'MemoryMax={g["hard_bytes"]}',str(PY),str(W/'scripts/c63_idle_run.py'),
            str(PY),str(Path(__file__).resolve()),'group',str(root),str(i)]
        with (root/'logs'/f'group-{i}.log').open('x') as h:
            return subprocess.call(command,env=environment(),cwd=W,stdout=h,stderr=subprocess.STDOUT)
    # One controller at a time owns this task-wide memory budget. The first
    # complete group profiles every variant serially before concurrency opens.
    import fcntl
    lock=(P/'c69-matrix.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with cf.ThreadPoolExecutor(max_workers=32) as pool:
        while active or cursor<len(jobs):
            reserved=sum(jobs[x['index']]['hard_bytes'] for x in active.values())
            capacity=32 if finished else 1
            while cursor<len(jobs) and len(active)<capacity and reserved+jobs[cursor]['hard_bytes']<=available()//2:
                active[pool.submit(launch,cursor)]=dict(index=cursor,started=time.time())
                reserved+=jobs[cursor]['hard_bytes'];cursor+=1
            done,_=cf.wait(active,timeout=15,return_when=cf.FIRST_COMPLETED) if active else ([],[])
            for future in done:
                meta=active.pop(future)
                try:meta['returncode']=future.result()
                except Exception as exc:meta.update(returncode=-1,error=repr(exc))
                finished.append(meta)
                if meta['index']==0:
                    profile=[read(root/'terminal'/f'0-{v}.json') for v in jobs[0]['order']
                             if (root/'terminal'/f'0-{v}.json').exists()]
                    valid=(meta['returncode']==0 and len(profile)==len(jobs[0]['order']) and
                           all(r['status']=='complete' and 0<r.get('peak_process_rss_bytes',0)<jobs[0]['hard_bytes'] for r in profile))
                    write(root/'profile.json',dict(passed=valid,records=profile,hard_bytes=jobs[0]['hard_bytes']))
                    assert valid,'first serial group failed validation/RSS profiling; no parallel admission'
            records=[read(f) for f in (root/'terminal').glob('*.json')]
            write(root/'status.json',dict(total=plan['units'],terminal_units=len(records),
                completed=sum(r['status']=='complete' for r in records),failures=[r for r in records if r['status']!='complete'],
                active=list(active.values()),finished_groups=finished,mem_available_bytes=available(),reserved_bytes=reserved))
            if not active and cursor<len(jobs):time.sleep(15)
    records=[read(f) for f in (root/'terminal').glob('*.json')]
    write(root/'result.json',dict(total=plan['units'],records=records,groups=finished,
        complete=len(records)==plan['units'],source_commit=plan['source_commit']))

if __name__=='__main__':
    if sys.argv[1]=='group':group(Path(sys.argv[2]),int(sys.argv[3]))
    else:
        p=argparse.ArgumentParser();p.add_argument('stage',choices=['formal','ablation','bird']);p.add_argument('out',type=Path)
        p.add_argument('--budget-policy',choices=['c64','600'],required=True)
        a=p.parse_args();run(a.out,a.stage,a.budget_policy)
