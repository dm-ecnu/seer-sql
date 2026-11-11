"""Post-arrival class proof tier; all SAT evidence passes the existing DBMS gate."""
from __future__ import annotations
import itertools
import math
import time
from .class_search import ClassWorker
from .representative_search import digest
from .stream import Edge
from .stream_numeric import bounded_numeric_admission
from .class_ablation import decision_pool


def _native_proof(engine, q, rep, cutoff):
    """Unchanged M4 SQLSolver admission, before the Polygon class step."""
    prover=engine.prover
    if prover is None:return False
    if hasattr(prover,'can_admit') and not prover.can_admit(engine.queries[q],engine.queries[rep]):
        engine.metrics['prover_skipped_inadmissible']+=1
        return False
    deadline=min(cutoff,time.perf_counter()+engine.per_call_s)
    if deadline<=time.perf_counter():return False
    engine.metrics['prover_calls']+=1
    started=time.perf_counter()
    native=prover(engine.queries[q],engine.queries[rep],deadline-started)
    engine.metrics['bounded_proof_s']+=time.perf_counter()-started
    engine.events.append({'event':'native_prover','query':q,'representative':rep,'receipt':native})
    if time.perf_counter()>=deadline:
        engine.metrics['late_evidence_rejected']+=1
        return False
    if native.get('status')=='native_eq' and engine.prover_admit is not None and engine.prover_admit(q,rep,native):
        return engine._add_edge(Edge(q,rep,'proved',native),deadline)
    return False


def prove_classes(engine, group_size=32, worker_factory=ClassWorker, class_off=False):
    assert group_size == 32, 'frozen class chunk default'
    search = engine.search
    search.close_worker()
    metrics = engine.metrics
    metrics.update(class_contexts=0,class_checks=0,class_sat_splits=0,
                   class_chunk_halvings=0,class_members_certified=0,
                   class_encoding_s=0.,class_solver_s=0.,class_pair_requests=0)
    if not engine.single_bucket_target:
        metrics.update(objective_rounds=0,objective_extra_separations=0,
                       objective_candidates=0)
    finished = set()
    while engine.remaining() > engine.finalization_reserve_s:
        with decision_pool(engine, 'class_context'):
            candidates = [(fp, list(m)) for fp,m in engine.index.items()
                          if len(m)>1 and (engine.no_certificate_transfer or tuple(m) not in finished)]
            if engine.no_certificate_transfer:
                candidates = [(fp, m[i:]) for fp, m in candidates for i in range(len(m)-1)
                              if tuple(m[i:]) not in finished]
            if not candidates:break
            fp, original = max(candidates,key=lambda c:len(c[1]))
            rep = original[0] if engine.no_certificate_transfer else engine.representatives[fp]
            members = [q for q in original if q!=rep]
            # A class gets the original per-call allowance for each initial chunk;
            # every individual solver request is capped by that same per-call limit.
            # No per-member restart after a timeout and no increase to batch B.
            cutoff = min(engine.deadline-engine.finalization_reserve_s,
                         time.perf_counter()+engine.per_call_s*math.ceil(len(members)/group_size))
            requested=set()
            def request(qids):
                fresh=set(qids)-requested
                requested.update(fresh)
                # Same metric unit as the frozen pairwise runner: a representative
                # PAIR request. Batching 32 members is not reported as one pair.
                metrics['pair_decision_requests']+=len(fresh)
                metrics['class_pair_requests']+=len(fresh)
            for q in members:
                if time.perf_counter()>=cutoff:break
                prover=engine.prover
                if prover is not None and (not hasattr(prover,'can_admit') or
                        prover.can_admit(engine.queries[q],engine.queries[rep])):
                    request([q])
                    _native_proof(engine,q,rep,cutoff)
            members=[q for q in members if engine._path(q,rep) is None]
            if not members or time.perf_counter()>=cutoff:
                finished.add(tuple(original))
                continue
            worker = worker_factory(search,rep)
            metrics['class_contexts'] += 1
            # Only the disjunction width changes. Keep the same class allowance,
            # native prover, context, capacities, admission and global SAT feedback.
            size=1 if class_off else group_size; stopped=False
            admission = {q:bounded_numeric_admission(search.irs.get(q),search.irs.get(rep),
                         engine.dialect,engine.schema) for q in members}
            try:
                for k in (engine.bounds if engine.bounds is not None else (2**i for i in itertools.count())):
                    pending = [q for q in members if engine.fingerprints[q]==engine.fingerprints[rep]]
                    if not pending:break
                    while pending:
                        seconds=min(engine.per_call_s,cutoff-time.perf_counter())
                        if seconds<=.2:stopped=True;break
                        chunk=pending[:size]
                        request(chunk)
                        stamp=time.perf_counter()
                        if engine.single_bucket_target:
                            receipt=worker.check(chunk,k,max(.01,seconds-.1))
                        else:
                            reps=list(engine.representatives.values())
                            pairs=list(zip(reps,reps[1:]))
                            if pairs:
                                receipt=worker.check(chunk,k,max(.01,seconds-.1),pairs)
                            else:
                                receipt=worker.check(chunk,k,max(.01,seconds-.1))
                            metrics['objective_rounds']+=receipt.get('objective_rounds',0)
                            metrics['objective_extra_separations']+=receipt.get('objective_extra_separations',0)
                            metrics['objective_candidates']+=receipt['status']=='candidate'
                        metrics['bounded_proof_s'] += time.perf_counter()-stamp
                        metrics['class_checks']+=receipt['status']!='reduce_chunk'
                        metrics['solver_calls']+=receipt.get('solver_calls',0)
                        metrics['class_encoding_s']+=receipt.get('encoding_s',0.)
                        metrics['class_solver_s']+=receipt.get('solve_s',0.)
                        engine.events.append({'event':'class_bounded_search','receipt':receipt,
                                              'wall_s':time.perf_counter()-engine.started})
                        if receipt['status']=='reduce_chunk':
                            assert size>1
                            size=max(1,size//2);metrics['class_chunk_halvings']+=1
                            continue
                        if receipt['status']=='candidate':
                            db=receipt['database']
                            assert not engine.schema.violations(db),'class split violates Sigma'
                            target=(receipt['differing'][0],rep)
                            before={q:engine.fingerprints[q] for q in original}
                            if not engine._install(db,target=target,admission_deadline=cutoff,origin='polygon'):
                                # A symbolic model may fail numeric/DBMS semantics.
                                # It must never be used as a split or retried forever.
                                stopped=True
                                engine.events.append({'event':'class_candidate_rejected','k':k,'target':target})
                                break
                            moved=[q for q in original if before[q]==before[rep]
                                   and engine.fingerprints[q]!=engine.fingerprints[rep]]
                            assert moved,'accepted SAT must split at least one member'
                            w=engine.witnesses[-1]
                            assert w.active
                            for q in original:
                                if before[q]==before[rep] and engine._wkey(w,q)!=engine._wkey(w,rep):
                                    assert engine.fingerprints[q]!=engine.fingerprints[rep], 'split missed a differing member'
                            metrics['class_sat_splits']+=1
                            engine.events.append({'event':'class_split_validated','k':k,'representative':rep,
                                                  'moved':moved,'sigma':True,'replay':True})
                            # Same depth, same solver; pool split removes ALL differing
                            # members, including those outside the current chunk.
                            pending=[q for q in pending if engine.fingerprints[q]==engine.fingerprints[rep]]
                            continue
                        if receipt.get('bounded_k'):
                            for q in receipt['encoded_members']:
                                gate=admission[q]
                                if gate.get('reason')=='numeric_row_bound_missing':
                                    gate=bounded_numeric_admission(search.irs[q],search.irs[rep],engine.dialect,engine.schema,k)
                                if not gate['admitted']:
                                    engine.pending_reason[q]=('undecided',gate['reason'])
                                    continue
                                pair_receipt=dict(receipt, query=q,query_sha256=digest(engine.queries[q]),
                                    representative_sha256=digest(engine.queries[rep]),numeric_admission=gate)
                                if engine._add_edge(Edge(q,rep,'bounded',pair_receipt,k,search.domain_identity()),cutoff):
                                    metrics['class_members_certified']+=1
                                    # Keep the greatest completed bound reachable; an
                                    # older direct edge must not hide this upgrade.
                                    obsolete=[e for e in engine.edges if e.status=='bounded'
                                        and {e.a,e.b}=={q,rep} and e.k<k
                                        and e.domain==search.domain_identity()]
                                    for edge in obsolete:
                                        engine.edges.remove(edge)
                                        for vertex in (edge.a,edge.b):engine.adjacency[vertex].remove(edge)
                            pending=pending[len(chunk):]
                        elif receipt['status']=='unsupported':
                            for q in chunk:engine.pending_reason[q]=('undecided','class_encoding_unsupported')
                            pending=pending[len(chunk):]
                        else:
                            stopped=True;break
                    if stopped:break
                    # Members that cannot be encoded at this capacity do not become
                    # encodable at a larger capacity. Avoid an unbounded empty loop.
                    if not receipt.get('bounded_k') and receipt['status']=='unsupported':break
            finally:
                worker.close()
            # Splits can create additional classes. Process those later, but don't
            # restart this representative's timed-out/finished context.
            finished.add(tuple(engine.index[engine.fingerprints[rep]]))
            finished.add(tuple(original))
            engine.events.append({'event':'class_proof_complete','representative':rep,
                                  'reason':'limit_or_unknown' if stopped else 'schedule_complete'})
