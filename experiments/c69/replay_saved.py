"""Independent C67/C68 double replay of C69 saved libraries; no search."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse,concurrent.futures as cf,importlib.util,os,subprocess,sys,time
from pathlib import Path
from receipts import read,sha,write,matrix
R=Path((SEER_ROOT));P=R/'repro';W=R/'seer-c69-objective-code'
PY=R/'venv-seer/bin/python';GIB=1024**3;HARD=4*GIB
OLD=R/'seer-dst-pair-xdata-v4-wt'
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
def available():
    return int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))*1024

def one(root,index):
    plan=read(root/'config.json');job=plan['jobs'][index]
    assert all(sha(p)==h for p,h in plan['source_sha256'].items())
    sys.path[:0]=[str(OLD/'src'),str(OLD/'eval'),str(OLD/'scripts')]
    import z3
    def forbidden(*a,**k):raise AssertionError('No solving during saved-library replay')
    z3.Solver.check=forbidden;z3.Optimize.check=forbidden
    d=Path(job['path']);r=read(d/'result.json');c=read(d/'config.json')
    assert r['config_sha256']==sha(d/'config.json') and sha(d/'result.json')==job['result_sha256']
    assert c['source_commit']==plan['source_commit']
    data=Path(job['input']);assert sha(data)==job['input_sha256']
    batch=next(b for b in map(__import__('json').loads,data.read_text().splitlines()) if b['batch_id']==job['batch'])
    if job['panel']=='leetcode':
        m=module('original_c68_replay',P/'c65-leetcode-detection.py')
        m.FORMAL=root/'source-view/leetcode';m.BASE=plan['source_commit']
        rec=m.seer_unit(batch,job['seed'])
        # C68 deduplicates original submitted SQL, not its SQLite translation.
        original=next(b for b in map(__import__('json').loads,
            (R/'datasets/batches/leetcode_verieql_original_batches.jsonl').read_text().splitlines())
            if b['batch_id']==job['batch'])
        sql={q['qid']:' '.join(q['sql'].split()) for q in original['queries']}
        rec['certified_sql_case_preserving']=sorted({sql[q] for q in rec['certified_qids']})
        unsupported=set(c['sqlite_conversion']['unsupported_qids'])
        rec['supported_reference_unique_pairs']=len({v for q,v in sql.items() if q!='gold' and q not in unsupported}) if 'gold' not in unsupported else 0
    else:
        assert job['panel']=='xdatabm'
        os.environ['SEER2_PG_DSN']='host=127.0.0.1 port=55932 dbname=postgres user=probe'
        m=module('original_c67_replay',P/'dstM3-cert-adapter-v2.py')
        rec=m.replay_keys('xdatabm',batch,r)
        assert set(rec['certified'])<=set(rec['label_killed'])
        sql={q['qid']:' '.join(q['sql'].split()) for q in batch['queries']}
        rec.update(status='complete',certified_sql_case_preserving=sorted({sql[q] for q in rec['certified']}))
    rec.update(panel=job['panel'],batch=job['batch'],seed=job['seed'],result_sha256=sha(d/'result.json'),
        config_sha256=sha(d/'config.json'),source_result=str(d/'result.json'),
        replay_affinity=sorted(os.sched_getaffinity(0)),solver_calls=0)
    assert rec['result_sha256']==job['result_sha256']
    assert all(sha(p)==h for p,h in plan['source_sha256'].items())
    write(root/'units'/f'{index}.json',rec)

def run(formal,root):
    import fcntl
    lock=(P/'c69-matrix.lock').open('a')
    if os.environ.get('C69_COORDINATED_REPLAY')!='1':fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    plan,units=matrix(formal,'formal',700)
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=OLD,text=True).strip()=='bb4b6bcb62b48c344601be957fbacb6ec7649e7c'
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=OLD,text=True).strip()
    # Require the exact replay sources whose old certification is already published.
    prior=read(P/'c65-leetcode-detection/config.json')
    assert all(sha(p)==h for p,h in prior['source_sha256'].items()),'C68 replay dependencies changed'
    prior_x=read(P/'c65-xdata-summary/saved-replay-v1/config.json')
    assert all(sha(p)==h for p,h in prior_x['source_hashes'].items()),'C67 replay dependencies changed'
    root.mkdir(parents=True,exist_ok=False)
    for n in ('units','logs','terminal'):(root/n).mkdir()
    grouped={(g['batch'],g['seed']):g for g in plan['groups']};jobs=[]
    for u in units:
        if u['status']!='complete':continue
        g=grouped[u['batch'],u['seed']]
        job={k:u[k] for k in ('panel','batch','seed','path','result_sha256')}
        job.update(input=g['input'],input_sha256=g['input_sha256']);jobs.append(job)
        # A read-only path view lets C68's original function run unchanged.
        alias=root/'source-view'/u['panel']/u['batch']/f'seer-s{u["seed"]}'
        alias.parent.mkdir(parents=True,exist_ok=True);alias.symlink_to(Path(u['path']),target_is_directory=True)
    paths=[Path(__file__),Path(__file__).with_name('receipts.py'),P/'c65-leetcode-detection.py',P/'dstM3-cert-adapter-v2.py',
           R/'datasets/batches/leetcode_verieql_original_batches.jsonl']
    hashes={**prior['source_sha256'],**prior_x['source_hashes'],**{str(p):sha(p) for p in paths}}
    write(root/'config.json',dict(source_commit=plan['source_commit'],source_sha256=hashes,jobs=jobs,
        expected_formal_units=700,source_failures=[{k:v for k,v in u.items() if k not in ('result','config')} for u in units if u['status']!='complete'],
        source_formal=str(formal),hard_limit_bytes=HARD,max_workers=4,
        scope='Same C67/C68 independent double replay; SQLite in-process and existing PostgreSQL only; no solver or method execution'))
    sys.path.insert(0,str(W/'scripts'));from c64_pipeline import environment
    def launch(i):
        with (root/'logs'/f'{i}.log').open('x') as h:
            rc=subprocess.call(['systemd-run','--user','--scope','--quiet',f'--unit={root.name}-{i}',
                '-p',f'MemoryMax={HARD}',str(PY),str(W/'scripts/c63_idle_run.py'),'prlimit',f'--as={HARD}',
                '--','/usr/bin/time','-v','-o',str(root/'logs'/f'{i}-rss.txt'),str(PY),str(Path(__file__).resolve()),
                'one',str(root),str(i)],cwd=W,env=environment(),stdout=h,stderr=subprocess.STDOUT)
        rec=dict(index=i,returncode=rc,unit=jobs[i]);write(root/'terminal'/f'{i}.json',rec);return rec
    done=[];active={};cursor=0;profiled=set()
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        while active or cursor<len(jobs):
            while cursor<len(jobs) and len(active)<4 and (len(active)+1)*HARD<=available()//2:
                panel=jobs[cursor]['panel']
                if panel not in profiled and active:break
                active[pool.submit(launch,cursor)]=cursor;cursor+=1
                if panel not in profiled:break
            finished,_=cf.wait(active,timeout=15,return_when=cf.FIRST_COMPLETED) if active else ([],[])
            for future in finished:
                i=active.pop(future);rec=future.result();done.append(rec)
                if rec['returncode']==0:profiled.add(jobs[i]['panel'])
            write(root/'status.json',dict(completed=len(done),expected=len(jobs),active=list(active.values()),
                failures=[r for r in done if r['returncode']],mem_available_bytes=available()))
            if not active and cursor<len(jobs):time.sleep(15)
    records=[read(p) for p in (root/'units').glob('*.json')]
    failed=[r for r in done if r['returncode']]
    assert not failed and len(records)==len(jobs),'replay failed; preserve logs and do not certify missing units'
    assert all(sha(p)==h for p,h in hashes.items())
    write(root/'result.json',dict(complete=True,source_commit=plan['source_commit'],total=len(jobs),units=records,
        source_failed_units=700-len(jobs),solver_calls=0,config_sha256=sha(root/'config.json')))

if __name__=='__main__':
    if sys.argv[1]=='one':one(Path(sys.argv[2]),int(sys.argv[3]))
    else:
        p=argparse.ArgumentParser();p.add_argument('formal',type=Path);p.add_argument('out',type=Path)
        a=p.parse_args();run(a.formal,a.out)
