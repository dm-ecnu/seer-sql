"""Read-only receipt validation shared by the C69 table builders."""
import hashlib,json
from pathlib import Path

def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,x):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_suffix('.tmp');t.write_text(json.dumps(x,indent=2,default=str)+'\n');t.replace(p)

def matrix(root,stage,expected):
    root=Path(root);plan=read(root/'config.json');terminal=read(root/'result.json')
    assert plan['stage']==stage and plan['units']==terminal['total']==expected
    assert terminal['complete'] and len(terminal['records'])==expected
    records=terminal['records'];keys={(r['panel'],r['batch'],r['seed'],r['variant']) for r in records}
    wanted={(g['panel'],g['batch'],g['seed'],v) for g in plan['groups'] for v in g['order']}
    assert keys==wanted and len(keys)==expected
    assert terminal['source_commit']==plan['source_commit']
    for i,g in enumerate(plan['groups']):
        assert sha(g['input'])==g['input_sha256'],'frozen input changed'
        group=read(root/'groups'/f'{i}.json');rs=group['records']
        assert [r['variant'] for r in rs]==g['order']
        assert len({r['core'] for r in rs})==1
        assert all(a['finished']<=b['started'] for a,b in zip(rs,rs[1:]))
    units=[]
    for r in records:
        item=dict(r)
        if r['status']=='complete':
            d=Path(r['path']);c=read(d/'config.json');result=read(d/'result.json')
            assert sha(d/'result.json')==r['result_sha256']
            assert result['config_sha256']==sha(d/'config.json')
            assert c['source_commit']==plan['source_commit'] and c['affinity']==[r['core']]
            assert (c['batch'],c['seed'])==(r['batch'],r['seed'])
            assert c['budget_s']==r['budget_s']
            item.update(config=c,result=result)
        units.append(item)
    return plan,units

def objective(units):
    ms=[u['result']['metrics'] for u in units if u['status']=='complete']
    interrupted=[u for u in units if u['status']=='complete' and any(
        e.get('event')=='class_bounded_search' and e['receipt'].get('reason') in ('class_hard_timeout','class_worker_exited')
        for e in u['result'].get('events',[]))]
    candidates=sum(m.get('objective_candidates',0) for m in ms)
    rounds=sum(m.get('objective_rounds',0) for m in ms)
    extra=sum(m.get('objective_extra_separations',0) for m in ms)
    return dict(candidates=candidates,rounds=rounds,extra_separations=extra,
        extra_per_candidate=extra/candidates if candidates else None,
        extra_per_round=extra/rounds if rounds else None,
        units_with_interrupted_class_receipts=len(interrupted),
        scope='Additional adjacent-representative separations satisfied by the final SMT model, compared with the initial SAT model; global DBMS admission is separate.',
        accounting='Returned class receipts; entry solver counters remain authoritative if a worker was interrupted before returning its detailed receipt.')
