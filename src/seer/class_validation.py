"""Development runner assertions against the independent, per-member Level path.

This validation is never counted as experimental method time. UNKNOWN is an
incomplete assertion, not a pass. All samples are deterministic and recorded.
"""
import hashlib
import time
import z3
from .ir import Unsupported
from .symbolic import Level
from .class_search import ClassContext
from .stream_numeric import bounded_numeric_admission


def individual(search, rep, q, k, seconds):
    try:
        return _individual(search,rep,q,k,seconds)
    except Unsupported as exc:
        return {'status':'unsupported','reason':exc.code}


def _individual(search, rep, q, k, seconds):
    deadline=time.perf_counter()+seconds
    lv=Level(search.schema,search.front,search.domain,
             {rep:search.irs[rep],q:search.irs[q]},k,search.dialect,
             max_cost=search.max_cost,deadline=deadline,timeout_ms=search.timeout_ms)
    if not(lv.ok(rep) and lv.ok(q)):
        return {'status':'unsupported'}
    answer,model=lv.check(z3.And(lv.valid([rep,q]),lv.sep(rep,q)))
    if answer==z3.unsat:
        unsafe=z3.Or(lv.err[rep],lv.err[q],
            *(z3.Not(lv.out[x].det) for x in (rep,q) if lv.out[x].det is not None))
        guard,_=lv.check(unsafe)
        if guard==z3.unsat:return {'status':'domain_unsat','bounded_k':k}
        return {'status':'undecided','guard':str(guard)}
    if answer==z3.sat:
        db=lv.decode(model,search.schema)
        assert not search.schema.violations(db)
        return {'status':'candidate'}
    return {'status':'unknown'}


def verify_bounded(engine, seconds=60.):
    edges={}
    for edge in engine.edges:
        if edge.status!='bounded':continue
        key=tuple(sorted((edge.a,edge.b)))
        if key not in edges or edge.k>edges[key].k:edges[key]=edge
    ordered=sorted(edges,key=lambda key:hashlib.sha256(repr(key).encode()).hexdigest())
    # Fixed sample, at least one and at most eight per workload; include the
    # deepest emitted receipt, which is the most exposed incremental state.
    keys=ordered[:min(8,max(1,(len(ordered)+9)//10))]
    if ordered:
        deepest=max(ordered,key=lambda key:edges[key].k)
        if deepest not in keys:keys.append(deepest)
    records=[]
    for key in keys:
        edge=edges[key]
        checked=individual(engine.search,edge.b,edge.a,edge.k,seconds)
        assert checked.get('bounded_k')==edge.k,(key,edge.k,checked)
        assert bounded_numeric_admission(engine.search.irs[edge.a],engine.search.irs[edge.b],
                                        engine.dialect,engine.schema,edge.k)['admitted']
        records.append({'a':edge.a,'b':edge.b,'k':edge.k,'individual':checked})
    for event in engine.events:
        if event['event']=='class_split_validated':
            assert event['sigma'] and event['replay'] and event['moved']
    return {'bounded_samples':records,'bounded_edges':len(edges),
            'validated_splits':sum(x['event']=='class_split_validated' for x in engine.events)}


def verify_singletons(search, pairs, bounds=(1,2), seconds=60.):
    records=[]
    for rep,q in pairs:
        if rep not in search.irs or q not in search.irs:
            records.append({'representative':rep,'query':q,'status':'front_unsupported'})
            continue
        context=ClassContext(search,rep)
        for k in bounds:
            current=context.check([q],k,seconds)
            old=individual(search,rep,q,k,seconds)
            # Compare the verdict and exact completed k, not incidental SAT
            # model choices, timings or Z3 search order.
            assert old['status']!='unknown' and old.get('guard')!='unknown',('individual_check_incomplete',rep,q,k)
            assert current['status']==old['status'],(rep,q,k,current,old)
            assert current.get('bounded_k')==old.get('bounded_k'),(rep,q,k,current,old)
            records.append({'representative':rep,'query':q,'k':k,
                            'status':old['status'],'bounded_k':old.get('bounded_k')})
    assert records,'no singleton comparison attempted'
    return records
