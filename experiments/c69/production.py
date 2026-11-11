"""One C69 resource budget for the three existing matrix runners and replay."""
import concurrent.futures as cf,fcntl,json,os,subprocess,sys,time
from pathlib import Path
import matrix as m
R,W,P,PY=m.R,m.W,m.P,m.PY
HERE=Path(__file__).resolve().parent
STAGES=('formal','ablation','bird');GIB=1024**3

def prepare(root,stage,frozen):
    jobs=m.groups(stage,'c64');root.mkdir(parents=True,exist_ok=False)
    for n in ('logs','terminal','groups','attempts'):(root/n).mkdir()
    files=[HERE/'matrix.py',HERE/'production.py',HERE/'score_bird_public_original_paired_v1.py']
    if stage=='bird':files += [HERE/(n+'.py') for n in ('run_bird_parseval','bird_parseval_adapter','parseval007_pool_worker')]
    plan=dict(stage=stage,budget_policy='c64',source_commit=frozen['source_commit'],groups=jobs,
        adapter_files={str(p):m.sha(p) for p in files},controller_sha256=m.sha(__file__),
        units=sum(len(g['order'])for g in jobs),pairing='Methods/variants adjacent on one physical-core lease; rotated order.',
        admission='All C69 stages share one controller: sum worker caps plus 2 GiB controller <= MemAvailable / 2.',
        retry_policy='One automatic same-protocol paired-group replay for positively identified infrastructure faults; original attempts retained. No retries for assertion, memory or timeout failures.')
    m.write(root/'config.json',plan)
    return dict(root=root,plan=plan,jobs=jobs,cursor=0,active=0,done=[],profiled=False,terminal=False,blocked=False,records={})

def execute(root,stage,index,hard):
    unit=f'c69-prod-{stage}-g{index}'
    cmd=['systemd-run','--user','--scope','--quiet',f'--unit={unit}','-p',f'MemoryMax={hard}',
         str(PY),str(W/'scripts/c63_idle_run.py'),str(PY),str(HERE/'matrix.py'),'group',str(root),str(index)]
    env=m.environment();env['PYTHONDONTWRITEBYTECODE']='1'
    with (root/'logs'/f'group-{index}.log').open('x') as h:return subprocess.call(cmd,cwd=W,env=env,stdout=h,stderr=subprocess.STDOUT)

def post(root,kind,hard):
    cmd=['systemd-run','--user','--scope','--quiet',f'--unit=c69-prod-{kind}','-p',f'MemoryMax={2*GIB if kind=="replay" else hard}',
        str(PY),str(W/'scripts/c63_idle_run.py'),'prlimit',f'--as={hard}','--',str(PY),str(HERE/'postprocess.py'),str(root),kind]
    env=m.environment();env.update(PYTHONDONTWRITEBYTECODE='1',C69_COORDINATED_REPLAY='1')
    with (root/'logs'/f'{kind}.log').open('x') as h:return subprocess.call(cmd,cwd=W,env=env,stdout=h,stderr=subprocess.STDOUT)

def main(root):
    lock=(P/'c69-matrix.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    frozen=m.freeze();root.mkdir(parents=True,exist_ok=False);(root/'logs').mkdir()
    stages={s:prepare(root/s,s,frozen)for s in STAGES}
    m.write(root/'config.json',dict(freeze=frozen,source_commit=frozen['source_commit'],controller_sha256=m.sha(__file__),
        formal_units=700,ablation_units=2520,bird_units=23010,budget_decision='codex-69.md supplement, explicitly confirmed by author continuation',
        memory_rule='worker hard limits + 2 GiB controller <= current MemAvailable / 2',max_concurrent_workers=32))
    active={};post_state={};turn=0
    with cf.ThreadPoolExecutor(max_workers=32) as pool:
        while True:
            reserved=sum(v['hard']for v in active.values())+2*GIB
            live=[s for s in STAGES if stages[s]['cursor']<len(stages[s]['jobs']) and not stages[s]['blocked']]
            share=max(1,32//max(1,len(live)))
            # Round-robin admission; each stage first measures its serial group.
            for _ in range(32*len(STAGES)):
                s=STAGES[turn%3];turn+=1;q=stages[s]
                cap=share if q['profiled'] else 1
                if q['terminal'] or q['blocked'] or q['cursor']>=len(q['jobs']) or q['active']>=cap or len(active)>=32:continue
                i=q['cursor'];hard=q['jobs'][i]['hard_bytes']
                if reserved+hard>m.available()//2:continue
                f=pool.submit(execute,q['root'],s,i,hard)
                active[f]=dict(stage=s,index=i,hard=hard,started=time.time());reserved+=hard;q['cursor']+=1;q['active']+=1
            prerequisites={'replay':stages['formal']['terminal'],
                'xdata_table':post_state.get('replay')==0,
                'formal_tables':post_state.get('replay')==0 and stages['ablation']['terminal'],'bird_table':stages['bird']['terminal']}
            for kind,ready in prerequisites.items():
                hard=(18 if kind=='replay' else 4)*GIB
                if ready and kind not in post_state and reserved+hard<=m.available()//2 and len(active)<32:
                    f=pool.submit(post,root,kind,hard);active[f]=dict(post=kind,hard=hard,started=time.time());reserved+=hard;post_state[kind]='running'
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
                reserved_hard_bytes=sum(v['hard']for v in active.values())+2*GIB,active=list(active.values()),postprocess=post_state,
                stages={s:dict(finished_groups=len(q['done']),total_groups=len(q['jobs']),blocked=q['blocked'],terminal=q['terminal'])for s,q in stages.items()}))
            if not active and all(q['terminal'] or q['blocked'] for q in stages.values()):
                if all(post_state.get(k) in (0,) for k in ('formal_tables','xdata_table','bird_table')) or any(q['blocked']for q in stages.values()) or any(isinstance(v,int)and v!=0 for v in post_state.values()):break
            if not active:time.sleep(15)
    m.write(root/'completion.json',dict(stages={s:dict(complete=q['terminal'],blocked=q['blocked'])for s,q in stages.items()},postprocess=post_state))

if __name__=='__main__':main(Path(sys.argv[1]))
