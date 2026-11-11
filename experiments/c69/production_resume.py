"""Resume C69 groups without replaying receipts; reserve ready postprocess first.

The original production.py and matrix.py remain byte-identical because unit
configs pin their hashes. This controller changes admission only.
"""
import concurrent.futures as cf,fcntl,json,os,subprocess,sys,time
from pathlib import Path
import matrix as m
from production import execute as original_execute,post
R,W,P,PY=m.R,m.W,m.P,m.PY
STAGES=('formal','ablation','bird');GIB=1024**3

def live_scopes():
    output=subprocess.check_output(['systemctl','--user','list-units','--type=scope',
        '--state=active,activating,deactivating','--no-legend','--plain','c69-prod-*.scope'],text=True)
    return {line.split()[0] for line in output.splitlines() if line.strip()}

def partition_work(total,completed,live,started):
    assert completed|live|started <= set(range(total))
    missing=started-completed-live
    assert not missing, f'Started groups without live scope or receipt: {sorted(missing)}; do not rerun'
    return sorted(completed-live),sorted(live),sorted(set(range(total))-completed-live)

def restore(root,stage,frozen,scopes):
    plan=m.read(root/'config.json');jobs=plan['groups']
    assert plan['source_commit']==frozen['source_commit']
    assert all(m.sha(p)==h for p,h in plan['adapter_files'].items())
    live={int(n.split('-g')[-1].split('.')[0]) for n in scopes if n.startswith(f'c69-prod-{stage}-g')}
    completed={int(p.stem) for p in (root/'groups').glob('*.json')}
    started={int(p.stem.split('-')[-1])for p in (root/'logs').glob('group-*.log')}
    done,adopt,pending=partition_work(len(jobs),completed,live,started)
    records={};meta=[]
    for i in done:
        g=m.read(root/'groups'/f'{i}.json');assert g['job']==jobs[i]
        assert len(g['records'])==len(jobs[i]['order'])
        for record in g['records']:records[i,record['variant']]=record
        meta.append(dict(stage=stage,index=i,hard=jobs[i]['hard_bytes'],returncode=0,resumed_receipt=True))
    terminal=(root/'result.json').exists()
    if terminal:
        prior=m.read(root/'result.json')
        assert prior['complete'] and len(done)==len(jobs) and not adopt and not pending
        assert len(records)==plan['units'];meta=prior['groups']
    profile=m.read(root/'profile.json') if (root/'profile.json').exists() else {}
    return dict(root=root,plan=plan,jobs=jobs,pending=pending,adopt=adopt,cursor=0,active=0,
        done=meta,profiled=profile.get('passed',False),terminal=terminal,blocked=False,records=records)

def wait_scope(unit,receipt):
    while True:
        state=subprocess.run(['systemctl','--user','show',unit,'-p','ActiveState','--value'],capture_output=True,text=True)
        if state.stdout.strip() not in ('active','activating','deactivating'):
            return 0 if receipt.exists() else 1
        time.sleep(5)

def post_admission(prerequisites,states,reserved,limit,slots):
    selected=[];waiting=False
    for kind,ready in prerequisites.items():
        if not ready or kind in states:continue
        hard=(18 if kind=='replay' else 4)*GIB
        if reserved+hard<=limit and slots>0:
            selected.append((kind,hard));reserved+=hard;slots-=1
        else:waiting=True
    return selected,waiting

def record_post(root,kind,hard):
    rc=post(root,kind,hard)
    m.write(root/'post-terminal'/f'{kind}.json',dict(returncode=rc,finished=time.time()))
    return rc

def execute(root,stage,index,hard):
    if stage!='bird' or not os.environ.get('C69_RESOURCE_POLICY'):
        return original_execute(root,stage,index,hard)
    cmd=['systemd-run','--user','--scope','--quiet',f'--unit=c69-prod-{stage}-g{index}',
        '-p',f'MemoryMax={hard}','-p','MemoryHigh=infinity',str(PY),str(W/'scripts/c63_idle_run.py'),
        str(PY),str(Path(__file__).with_name('group_resources.py')),str(root),str(index),os.environ['C69_RESOURCE_POLICY']]
    env=m.environment();env['PYTHONDONTWRITEBYTECODE']='1'
    with (root/'logs'/f'group-{index}.log').open('x') as h:
        return subprocess.call(cmd,cwd=W,env=env,stdout=h,stderr=subprocess.STDOUT)

def scope_hard(root,stage,index):
    try:
        value=int(subprocess.check_output(['systemctl','--user','show',f'c69-prod-{stage}-g{index}.scope',
            '-p','MemoryMax','--value'],text=True).strip())
        assert value>0
        return value
    except (ValueError,subprocess.CalledProcessError):
        # A completed scope may disappear after the initial inventory.
        resource=root/'resources'/f'{index}.json'
        if resource.exists():return m.read(resource)['effective_hard_bytes']
        return m.read(root/'config.json')['groups'][index]['hard_bytes']

def main(root):
    lock=(P/'c69-matrix.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    frozen=m.freeze();assert root.exists()
    assert m.read(root/'config.json')['source_commit']==frozen['source_commit']
    scopes=live_scopes()
    stages={s:restore(root/s,s,frozen,scopes)for s in STAGES}
    active={};post_state={};turn=0
    resource_path=os.environ.get('C69_RESOURCE_POLICY')
    resource=m.read(resource_path) if resource_path else {}
    max_workers=resource.get('max_concurrent_workers',32)
    assert 1<=max_workers<=60
    if resource:
        assert resource['source_commit']==frozen['source_commit']
        assert m.sha(__file__)==resource['controller_sha256']
    for path in (root/'post-terminal').glob('*.json'):
        post_state[path.stem]=m.read(path)['returncode']
    m.write(root/'scheduler-resume.json',dict(time=time.time(),controller_sha256=m.sha(__file__),resource_policy=resource_path,max_concurrent_workers=max_workers,
        adopted={s:q['adopt'] for s,q in stages.items()},completed={s:len(q['done']) for s,q in stages.items()},
        policy='Ready postprocess before fresh groups; wait for capacity without preempting active groups.'))
    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for s,q in stages.items():
            for i in q['adopt']:
                f=pool.submit(wait_scope,f'c69-prod-{s}-g{i}.scope',q['root']/'groups'/f'{i}.json')
                active[f]=dict(stage=s,index=i,hard=scope_hard(q['root'],s,i),started=time.time(),adopted=True)
                q['active']+=1
        while True:
            reserved=sum(v['hard']for v in active.values())+2*GIB
            live=[s for s in STAGES if stages[s]['cursor']<len(stages[s]['pending']) and not stages[s]['blocked']]
            share=max(1,max_workers//max(1,len(live)))
            prerequisites={'replay':stages['formal']['terminal'],
                'xdata_table':post_state.get('replay')==0,
                'formal_tables':post_state.get('replay')==0 and stages['ablation']['terminal'],'bird_table':stages['bird']['terminal']}
            admissions,waiting_post=post_admission(prerequisites,post_state,reserved,m.available()//2,max_workers-len(active))
            for kind,hard in admissions:
                f=pool.submit(record_post,root,kind,hard)
                active[f]=dict(post=kind,hard=hard,started=time.time());reserved+=hard;post_state[kind]='running'
            # No fresh method group may consume capacity needed by a ready post.
            for _ in range(max_workers*len(STAGES)):
                if waiting_post:break
                s=STAGES[turn%3];turn+=1;q=stages[s]
                cap=share if q['profiled'] else 1
                if q['terminal'] or q['blocked'] or q['cursor']>=len(q['pending']) or q['active']>=cap or len(active)>=max_workers:continue
                i=q['pending'][q['cursor']];hard=resource.get('bird_hard_bytes',q['jobs'][i]['hard_bytes']) if s=='bird' else q['jobs'][i]['hard_bytes']
                if reserved+hard>m.available()//2:continue
                f=pool.submit(execute,q['root'],s,i,hard)
                active[f]=dict(stage=s,index=i,hard=hard,started=time.time());reserved+=hard;q['cursor']+=1;q['active']+=1
            done,_=cf.wait(active,timeout=15,return_when=cf.FIRST_COMPLETED) if active else ([],[])
            for f in done:
                meta=active.pop(f)
                try:rc=f.result()
                except Exception as e:rc=-1;meta['error']=repr(e)
                if 'post' in meta:post_state[meta['post']]=rc;continue
                s=meta['stage'];q=stages[s];i=meta['index'];q['active']-=1;q['done'].append(dict(**meta,returncode=rc))
                gfile=q['root']/'groups'/f'{i}.json'
                if not gfile.exists():
                    q['blocked']=True;q['block_reason']=f'group {i} exited {rc} without a group receipt; original logs retained'
                    continue
                group=m.read(gfile)
                for record in group['records']:q['records'][i,record['variant']]=record
                if i==0:
                    rss=[r.get('peak_process_rss_bytes') for r in group['records']]
                    q['profiled']=len(rss)==len(q['jobs'][0]['order']) and all(v and v>0 for v in rss)
                    m.write(q['root']/'profile.json',dict(passed=q['profiled'],records=group['records'],hard_bytes=meta['hard']))
                    if not q['profiled']:q['blocked']=True;q['block_reason']='serial RSS profile unavailable'
            for s,q in stages.items():
                records=list(q['records'].values())
                if len(q['done'])==len(q['jobs']) and not q['active'] and not q['terminal'] and not q['blocked']:
                    attempts=[m.read(f) for f in (q['root']/'attempts').glob('*.json')]
                    count=sum(len(a['records'])for a in attempts);failed=sum(r['status']!='complete'for a in attempts for r in a['records'])
                    m.write(q['root']/'result.json',dict(total=q['plan']['units'],records=records,groups=q['done'],
                        complete=len(records)==q['plan']['units'],source_commit=frozen['source_commit'],
                        attempted_units=count,failed_attempts=failed,infrastructure_retry_groups=len(attempts)-len(q['jobs'])))
                    q['terminal']=True
                m.write(q['root']/'status.json',dict(total=q['plan']['units'],completed=sum(r['status']=='complete'for r in records),
                    failures=[r for r in records if r['status']!='complete'],terminal_units=len(records),active=[v for v in active.values()if v.get('stage')==s],
                    finished_groups=len(q['done']),profiled=q['profiled'],state='blocked' if q['blocked'] else 'complete' if q['terminal'] else 'running',block_reason=q.get('block_reason')))
            m.write(root/'status.json',dict(updated=time.time(),source_commit=frozen['source_commit'],mem_available_bytes=m.available(),
                max_concurrent_workers=max_workers,resource_policy=resource_path,
                reserved_hard_bytes=sum(v['hard']for v in active.values())+2*GIB,active=list(active.values()),postprocess=post_state,
                waiting_postprocess=[k for k,v in prerequisites.items() if v and k not in post_state],
                stages={s:dict(finished_groups=len(q['done']),total_groups=len(q['jobs']),blocked=q['blocked'],terminal=q['terminal'])for s,q in stages.items()}))
            if not active and all(q['terminal'] or q['blocked'] for q in stages.values()):
                if all(post_state.get(k) in (0,) for k in ('formal_tables','xdata_table','bird_table')) or any(q['blocked']for q in stages.values()) or any(isinstance(v,int)and v!=0 for v in post_state.values()):break
            if not active:time.sleep(15)
    m.write(root/'completion.json',dict(stages={s:dict(complete=q['terminal'],blocked=q['blocked'])for s,q in stages.items()},postprocess=post_state))

if __name__=='__main__':main(Path(sys.argv[1]))
