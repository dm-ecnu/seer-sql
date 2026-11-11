"""Frozen-roster method gate, then CPU-only SEER reruns. No opponent execution."""
from __future__ import annotations
import concurrent.futures as cf
import json,os,shlex,subprocess,sys,time
from pathlib import Path
from c63_consistency import R,P,PY,NEW,read,write,sha,selection,NAMES
NOTE=('SEER uses the class-level bounded method rerun; SQLWitness reuses the original '
      'm36 (LeetCode) / xdata-v4 receipts. These sides were not run adjacently in the same time period. '
      'Other panels retain their original efficiency measurements as conservative values.')


def environment():
    env=dict(os.environ,PYTHONPATH=f'{NEW}/src:{NEW}/eval:{NEW}/scripts',
        TMPDIR=str(R.parent/'tmp'),SQLITE_TMPDIR=str(R.parent/'tmp'),
        SEER_SQLWITNESS_SQLITE_SCRATCH=str(R.parent/'tmp'),
        OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',MALLOC_ARENA_MAX='2')
    # Read only the already deployed PostgreSQL endpoints; never log secrets.
    allowed={'SEER_PG_URL','SEER2_PG_DSN','SQLWITNESS_POSTGRES_URL','PG_URL'}
    for line in (R/'env2.sh').read_text().splitlines():
        line=line.removeprefix('export ').strip()
        key,sep,value=line.partition('=')
        if sep and key in allowed:
            parts=shlex.split(value)
            assert len(parts)==1
            env[key]=parts[0]
    for key in list(env):
        if 'MYSQL' in key:env.pop(key)
    return env


def roster():
    jobs=[]
    for panel in ('leetcode','xdatabm'):
        source=(P/'dstM3-sqlite-m36/inputs' if panel=='leetcode' else R/'datasets/batches')/NAMES[panel]
        base=P/'dstM3-sqlite-m36/main/leetcode' if panel=='leetcode' else P/'dstM3-pair-xdata-v4/xdatabm'
        batches=list(map(json.loads,source.read_text().splitlines()))
        assert len(batches)==(56 if panel=='leetcode' else 84)
        for b in batches:
            for seed in range(5):
                c=base/b['batch_id']/f'seer-s{seed}/config.json'
                cfg=read(c)
                assert cfg['budget_s']==(len(b['queries'])-1)*cfg['per_call_s']
                assert cfg['seed']==seed and cfg['query_ids']==[q['qid'] for q in b['queries']]
                jobs.append(dict(panel=panel,batch=b['batch_id'],seed=seed,input=str(source),
                    config=str(c),input_sha256=sha(source),old_config_sha256=sha(c),n=len(b['queries'])))
    assert len(jobs)==700
    return jobs


def command(job,out,kind):
    cfg=read(job['config'])
    if kind=='gate':
        inner=[str(PY),str(NEW/'scripts/c64_validate.py'),'--job',json.dumps(job),'--out',str(out)]
        limit=cfg['budget_s']+max(240,2*cfg['per_call_s']*4*job.get('n',10))
    else:
        runner='run_stream_batch_sqlite.py' if job['panel']=='leetcode' else 'run_stream_batch.py'
        inner=[str(PY),str(NEW/'scripts'/runner),'--dataset',job['input'],'--batch',job['batch'],
            '--method','seer_stream_sqlwitness','--seed',str(job['seed']),'--order',cfg['order'],
            '--schedule','class_bounded','--quick-schedule','compare',
            '--original-pair-timeout-s',str(cfg['per_call_s']),'--out',str(out)]
        if kind=='dev':inner.append('--validate-class-bounded')
        limit=cfg['budget_s']+240+(9*cfg['per_call_s'] if kind=='dev' else 0)
    return [str(PY),str(NEW/'scripts/c63_idle_run.py'),'prlimit','--as=6442450944','--',
            'timeout','--signal=TERM','--kill-after=5s',str(limit),*inner]


def available():
    return int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))/1024**2


def execute(jobs,root,lanes):
    root.mkdir(parents=True,exist_ok=False)
    (root/'logs').mkdir()
    expected=subprocess.check_output(['git','rev-parse','HEAD'],cwd=NEW,text=True).strip()
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=NEW,text=True).strip()
    write(root/'config.json',dict(jobs=jobs,source_commit=expected,table_note=NOTE,
        max_concurrent=lanes,memory_reserve_gib=128,per_lane_reserve_gib=24,development_only=any(j['kind']!='formal' for j in jobs)))
    done=[];failed=[];active={};waiting=iter(jobs);exhausted=False
    def one(j):
        assert sha(j['input'])==j['input_sha256'] and sha(j['config'])==j['old_config_sha256']
        dest=root/j['kind']/j['panel']/j['batch']/f'seer-s{j["seed"]}'
        log=root/'logs'/f'{j["kind"]}-{j["batch"]}-s{j["seed"]}.log'
        with log.open('x') as h:
            rc=subprocess.call(command(j,dest,j['kind']),env=environment(),stdout=h,stderr=subprocess.STDOUT)
        if rc==0:
            result=read(dest/'result.json')
            if j['kind']=='gate':assert result['assertions_passed']
            if j['kind']=='dev':
                assert result['class_assertions']['passed']
                assert result['metrics'].get('microbatch_success_rows',0)>0, 'no successful official microbatch rows'
            config=read(dest/'config.json');assert config['source_commit']==expected
            if j['kind']!='gate':
                old=read(j['config']);assert config['budget_s']==old['budget_s'] and config['per_call_s']==old['per_call_s']
                assert config['query_ids']==old['query_ids']
        return rc
    with cf.ThreadPoolExecutor(max_workers=lanes) as pool:
        while active or not exhausted:
            capacity=min(lanes,max(0,int((available()-128)//24)))
            while not exhausted and not failed and len(active)<capacity:
                j=next(waiting,None)
                if j is None:exhausted=True;break
                active[pool.submit(one,j)]=dict(job=j,started=time.time())
            finished,_=cf.wait(active,timeout=15,return_when=cf.FIRST_COMPLETED) if active else ([],[])
            for future in finished:
                rec=active.pop(future)
                try:rc=future.result()
                except Exception as exc:rc=-1;rec['error']=repr(exc)
                rec['returncode']=rc
                (done if rc==0 else failed).append(rec)
            if failed:exhausted=True
            write(root/'status.json',dict(state='running',completed=len(done),failed=failed,
                 total=len(jobs),active=list(active.values()),mem_available_gib=available()))
            if not active and not exhausted:time.sleep(15)
    value=dict(passed=not failed and len(done)==len(jobs),completed=done,failed=failed,total=len(jobs),
               source_commit=expected,table_note=NOTE)
    write(root/'result.json',value);return value


def main(root):
    jobs=roster()
    chosen={'1132','1251','1661','1777','1084','1581','607'}
    development=[dict(j,kind='dev') for j in jobs if j['panel']=='leetcode' and j['seed']==0
                 and j['batch'].split('-')[-1] in chosen]
    assert len(development)==7
    checks=[dict(j,kind='gate') for j in selection() if j['panel'] in ('spider','bird_sw','calcite','literature')]
    assert len(checks)==80
    # Keep the original frozen sample, including unsupported conversion cases.
    # Add hash-ordered frozen loads until each panel has 20 actual query pairs.
    import hashlib
    for panel in ('spider','bird_sw','calcite','literature'):
        selected=[j for j in checks if j['panel']==panel]
        source=Path(selected[0]['input'])
        batches={b['batch_id']:b for b in map(json.loads,source.read_text().splitlines())}
        def executable(b):
            return len(b['queries'])-len(b.get('meta',{}).get('sqlite_conversion',{}).get('unsupported_qids',[]))
        count=sum(executable(batches[j['batch']])>=2 for j in selected)
        base=Path(selected[0]['config']).parents[2]
        existing={j['batch'] for j in selected}
        for bid in sorted(batches,key=lambda b:hashlib.sha256(('codex63-v1:'+b).encode()).hexdigest()):
            if count>=20:break
            if bid in existing or executable(batches[bid])<2:continue
            cfg=base/bid/'seer-s0/config.json'
            if not cfg.exists():continue
            checks.append(dict(panel=panel,batch=bid,seed=0,input=str(source),config=str(cfg),
                input_sha256=sha(source),old_config_sha256=sha(cfg),kind='gate',
                sampling_reason='supplement frozen conversion cases with no candidate'))
            count+=1
        assert count>=20
    # Interleave long development cases with the frozen small-panel assertions.
    scheduled=[]
    for i in range(7):scheduled.extend(development[i:i+1]+checks[i*12:(i+1)*12])
    scheduled.extend(checks[84:])
    result=execute(scheduled,root/'development',4)
    if not result['passed']:
        print('Class-method development gate failed; formal reruns are not started.',flush=True);return
    for panel in ('spider','bird_sw','calcite','literature'):
        assert sum(r['job']['kind']=='gate' and r['job']['panel']==panel for r in result['completed'])>=20
    # The tested commit is the frozen commit; tagging creates no source changes.
    commit=result['source_commit']
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=NEW,text=True).strip()==commit
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=NEW,text=True).strip()
    subprocess.run(['git','tag',f'c64-class-bounded-{commit[:12]}',commit],cwd=NEW,check=True)
    write(root/'freeze.json',dict(source_commit=commit,gate_sha256=sha(root/'development/result.json'),
                                frozen_at=time.time(),table_note=NOTE))
    formal=[dict(j,kind='formal') for j in jobs]
    # MemoryMax is applied to the whole controller and descendants. No MemoryHigh.
    subprocess.run(['systemd-run','--user','--unit='+root.name+'-formal',
        '-p','MemoryMax=192G','-p','WorkingDirectory='+str(NEW),str(PY),str(Path(__file__).resolve()),
        'formal',str(root)],check=True)


if __name__=='__main__':
    root=Path(sys.argv[2]);root.mkdir(parents=True,exist_ok=True)
    if sys.argv[1]=='formal':
        frozen=read(root/'freeze.json')
        assert frozen['source_commit']==subprocess.check_output(['git','rev-parse','HEAD'],cwd=NEW,text=True).strip()
        assert read(root/'development/result.json')['passed']
        execute([dict(j,kind='formal') for j in roster()],root/'formal',8)
    else:main(root)
