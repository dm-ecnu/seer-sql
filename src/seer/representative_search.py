"""Incremental representative search over the existing Polygon-derived Level.

SAT databases require independent engine admission. UNSAT receipts explicitly
retain the encoded value domain and valid-execution restriction; they are not
an unrestricted bounded@k proof for the final online partition.
"""
from __future__ import annotations
import hashlib
import itertools
import math
import multiprocessing as mp
from dataclasses import asdict
from pathlib import Path
import json
import time
import z3
from .encode import DomainInputs, schema_strings
from .front import Front
from .ir import Unsupported
from .symbolic import Level, est_cost


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class IncrementalLevel(Level):
    def extend(self, qid, relation, max_cost=400000):
        if qid in self.irs:
            raise ValueError('query already registered at this level')
        if est_cost(relation, self.k, self.enc.S) > max_cost:
            raise Unsupported('encoding_too_large', 'query exceeds existing encoding cost guard')
        side_start = len(self.enc.side)
        self.enc.errs = []
        output = self.enc.rel(relation)
        from .encode import _or
        self.solver.add(self.enc.side[side_start:])
        self.out[qid] = output
        self.err[qid] = _or(self.enc.errs)
        self.irs[qid] = relation


def _search_in_child(search, args, pipe):
    try:
        pipe.send(('ok', search._search_inner(*args)))
    except BaseException as exc:
        pipe.send(('error', f'{type(exc).__name__}: {exc}'))
    finally:
        pipe.close()


def _persistent_search_worker(schema, dialect, seed, timeout_ms, max_cost, max_side_constraints, pipe):
    search = RepresentativeSearch(schema, dialect, seed, timeout_ms, max_cost,
                                  max_side_constraints=max_side_constraints)
    try:
        while True:
            command, args = pipe.recv()
            if command == 'stop':
                break
            try:
                if command == 'add':
                    search.add_query(*args)
                    pipe.send(('ok', None))
                elif command == 'search':
                    pipe.send(('ok', search._search_inner(*args, progress=lambda receipt: pipe.send(('progress', receipt)))))
                else:
                    raise ValueError(f'unknown search command: {command}')
            except z3.Z3Exception as exc:
                if 'canceled' in str(exc).casefold() or 'interrupted' in str(exc).casefold():
                    pipe.send(('canceled', str(exc)))
                else:
                    pipe.send(('error', f'{type(exc).__name__}: {exc}'))
            except BaseException as exc:
                pipe.send(('error', f'{type(exc).__name__}: {exc}'))
    except EOFError:
        pass
    finally:
        pipe.close()


class RepresentativeSearch:
    def __init__(self, schema, dialect, seed=0, timeout_ms=20000, max_cost=400000, max_side_constraints=50000, hard_timeout=False, persistent_worker=False):
        self.schema, self.dialect = schema, dialect
        self.seed, self.timeout_ms, self.max_cost = seed, timeout_ms, max_cost
        self.max_side_constraints = max_side_constraints
        self.hard_timeout = hard_timeout
        self.persistent_worker = persistent_worker
        self._worker = None
        self._worker_pipe = None
        self._worker_queries = []
        self._level_workers = {}
        self.front = Front(schema, dialect)
        self.ir_pool = {}
        self.queries, self.irs, self.unsupported = {}, {}, {}
        self.ir_order = []
        self.levels = {}
        self.domain_inputs = DomainInputs(checks_strings=schema_strings(schema))
        self.domain = self.domain_inputs.build()
        self.domain_revision = 0
        self.trace = []

    def domain_identity(self):
        return digest({'strings': self.domain.strings, 'date_days': [self.domain.dlo,self.domain.dhi]})

    def add_query(self, qid, sql):
        if qid in self.queries:
            raise ValueError('duplicate query identity')
        self.queries[qid] = sql
        try:
            from .ir import intern_tree
            self.irs[qid] = intern_tree(self.front.translate(sql), self.ir_pool)
        except Unsupported as e:
            self.unsupported[qid] = e.code
            return
        except RecursionError:
            # Deep or cyclic source input is unsupported for this front end;
            # it must not abort the remaining arrival stream.
            self.unsupported[qid] = 'front_recursion_limit'
            return
        self.ir_order.append(qid)
        self.domain_inputs.add([self.irs[qid]])
        domain = self.domain_inputs.build()
        old = self.domain_identity()
        new = digest({'strings': domain.strings, 'date_days': [domain.dlo,domain.dhi]})
        if old != new:
            self.domain = domain
            self.domain_revision += 1
            self.levels.clear()
            self.trace.append({'event':'domain_rebuilt','revision':self.domain_revision,'identity':new})

    def _level(self, k, deadline):
        reused = k in self.levels
        if not reused:
            self.levels[k] = IncrementalLevel(self.schema,self.front,self.domain,dict(self.irs),k,
                self.dialect, timeout_ms=self.timeout_ms,max_cost=self.max_cost,seed=self.seed,deadline=deadline)
            self.levels[k]._stream_registered = len(self.ir_order)
        level = self.levels[k]
        level.deadline = level.enc.deadline = deadline
        try:
            for qid in self.ir_order[level._stream_registered:]:
                level.extend(qid,self.irs[qid],self.max_cost)
            level._stream_registered = len(self.ir_order)
        except (Unsupported, TimeoutError, z3.Z3Exception, RecursionError):
            # A partially encoded query may have appended constraints/memo state.
            # Rebuild before any result can use that state.
            del self.levels[k]
            if time.perf_counter() >= deadline:
                raise
            return self._level(k, deadline)[0], False
        return level, reused

    def _close_active_worker(self):
        proc, pipe = self._worker, self._worker_pipe
        self._worker = self._worker_pipe = None
        self._worker_queries = []
        if proc is None:
            return
        try:
            if proc.is_alive():
                try:
                    pipe.send(('stop', ()))
                except (BrokenPipeError, OSError):
                    pass
                proc.join(0.1)
            if proc.is_alive():
                proc.terminate()
                proc.join(0.2)
            if proc.is_alive():
                proc.kill()
                proc.join()
        finally:
            pipe.close()

    def _start_worker(self):
        context = mp.get_context('fork')
        parent, child = context.Pipe()
        proc = context.Process(target=_persistent_search_worker,
                               args=(self.schema, self.dialect, self.seed, self.timeout_ms,
                                     self.max_cost, self.max_side_constraints, child))
        proc.daemon = True
        proc.start()
        child.close()
        self._worker, self._worker_pipe = proc, parent
        self._worker_queries = []

    def _search_persistent_level(self, query, representative, budget_s, bounds,
                           representative_pairs, augment, canonical_contract, reserve=None):
        if not math.isfinite(budget_s):
            raise ValueError('finite remaining budget required')
        if budget_s <= 0:
            return self._search_inner(query, representative, budget_s, bounds,
                                      representative_pairs, augment, canonical_contract)
        start = time.perf_counter()
        deadline = start + budget_s
        desired = list(self.queries.items())
        if self._worker is not None and desired[:len(self._worker_queries)] != self._worker_queries:
            self._close_active_worker()
        if self._worker is None or not self._worker.is_alive():
            self._close_active_worker()
            self._start_worker()
        args=(query, representative, budget_s, bounds, representative_pairs, augment, canonical_contract)
        try:
            for item in desired[len(self._worker_queries):]:
                self._worker_pipe.send(('add', item))
                left = deadline - time.perf_counter()
                if left <= 0 or not self._worker_pipe.poll(left):
                    return self._worker_timeout_receipt(query, representative, canonical_contract, start)
                kind, value = self._worker_pipe.recv()
                if kind != 'ok':
                    raise RuntimeError(f'symbolic worker add failed: {kind}: {value}')
                self._worker_queries.append(item)
            left = deadline - time.perf_counter()
            if left <= 0:
                return self._worker_timeout_receipt(query, representative, canonical_contract, start)
            self._worker_pipe.send(('search', args))
            progress = None
            reserve = min(1.0, max(0.01, budget_s * 0.05)) if reserve is None else reserve
            while True:
                left = deadline - time.perf_counter() - reserve
                if left <= 0 or not self._worker_pipe.poll(left):
                    return self._worker_timeout_receipt(query, representative, canonical_contract,
                                                        start, progress=progress)
                kind, value = self._worker_pipe.recv()
                if kind == 'progress':
                    assert value.get('bounded_k') and value.get('domain_sha256') == self.domain_identity()
                    progress = value
                    continue
                if kind == 'canceled':
                    return self._worker_timeout_receipt(query, representative, canonical_contract,
                                                        start, reason='solver_canceled', progress=progress)
                if kind != 'ok':
                    raise RuntimeError(f'symbolic worker failed: {kind}: {value}')
                return value
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._close_active_worker()
            raise RuntimeError(f'symbolic worker pipe failed: {exc}') from exc

    def close_worker(self):
        self._close_active_worker()
        for state in self._level_workers.values():
            self._worker, self._worker_pipe, self._worker_queries = state
            self._close_active_worker()
        self._level_workers.clear()

    def _search_persistent(self, query, representative, budget_s, bounds,
                           representative_pairs, augment, canonical_contract):
        """Retain completed depth workers when a deeper encoding is killed.

        Each depth uses the existing incremental encoder, query registration,
        solver assumptions, and domain invalidation. Only one worker executes
        at a time; every call shares the original absolute wall deadline.
        No timeout is reused as a result for another query.
        """
        if not math.isfinite(budget_s):
            raise ValueError('finite remaining budget required')
        if budget_s <= 0:
            return self._search_inner(query, representative, budget_s, bounds,
                                      representative_pairs, augment, canonical_contract)
        if bounds is not None and (not bounds or any(type(k) is not int or k < 1 for k in bounds)
                                   or list(bounds) != sorted(set(bounds))):
            raise ValueError('bounds must be a nonempty increasing positive schedule')
        start = time.perf_counter()
        deadline = start + budget_s
        schedule = bounds if bounds is not None else (2**i for i in itertools.count())
        # Preserve the original call-wide cleanup allowance. Recomputing it
        # from each depth's shrinking remaining budget can return a valid
        # prefix after the caller has stopped accepting certificates.
        reserve = min(1.0, max(0.01, budget_s * 0.05))
        levels, bounded_k, last = [], None, None
        for k in schedule:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                if last is None:
                    return self._search_inner(query, representative, 0, bounds,
                                              representative_pairs, augment, canonical_contract)
                return {**last, 'status': 'undecided', 'reason': 'budget',
                        'wall_s': time.perf_counter() - start}
            self._worker, self._worker_pipe, self._worker_queries = self._level_workers.pop(
                k, (None, None, []))
            try:
                value = self._search_persistent_level(
                    query, representative, remaining, (k,), representative_pairs,
                    augment, canonical_contract, reserve=reserve)
            finally:
                if self._worker is not None:
                    self._level_workers[k] = (self._worker, self._worker_pipe, self._worker_queries)
                self._worker, self._worker_pipe, self._worker_queries = None, None, []
            levels.extend(value.get('levels', []))
            bounded_k = value.get('bounded_k') or bounded_k
            # A killed deeper worker may return only a timeout header. Retain
            # the last completed prefix's query/schema/domain/scope evidence;
            # never attach a bound to an anonymous timeout receipt.
            last = {**(last or {}), **value, 'levels': list(levels), 'bounded_k': bounded_k,
                    'wall_s': time.perf_counter() - start}
            if value['status'] != 'domain_unsat' and 'k' not in value:
                last.pop('k', None)
            if value['status'] != 'domain_unsat':
                if bounded_k and value.get('reason') in ('hard_timeout', 'solver_canceled'):
                    last['reason'] = value['reason'] + '_after_bounded_progress'
                return last
        return last

    def _worker_timeout_receipt(self, query, representative, canonical_contract, start, reason='hard_timeout', progress=None):
        self._close_active_worker()
        if progress is not None:
            return {**progress, 'status':'undecided', 'reason':reason+'_after_bounded_progress',
                    'wall_s':time.perf_counter()-start}
        return {'query':query,'representative':representative,'status':'undecided',
                'reason':reason,'levels':[],'proof_admitted':False,
                'bounded_k':None,'domain_sha256':self.domain_identity(),
                'comparison_contract':'canonical-exact-decimal-float3dp-list-bag-errors-v3' if canonical_contract else None,
                'wall_s':time.perf_counter()-start}

    def search(self, query, representative, budget_s, bounds=None, representative_pairs=(), augment=3, canonical_contract=False):
        if self.persistent_worker:
            return self._search_persistent(query, representative, budget_s, bounds,
                                           representative_pairs, augment, canonical_contract)
        if not self.hard_timeout or budget_s <= 0:
            return self._search_inner(query, representative, budget_s, bounds,
                                      representative_pairs, augment, canonical_contract)
        if not math.isfinite(budget_s):
            raise ValueError('finite remaining budget required')
        args=(query, representative, budget_s, bounds, representative_pairs, augment, canonical_contract)
        parent, child = mp.get_context('fork').Pipe(duplex=False)
        proc = mp.get_context('fork').Process(target=_search_in_child, args=(self, args, child))
        proc.start()
        child.close()
        try:
            if parent.poll(budget_s):
                try:
                    kind, result = parent.recv()
                except EOFError as exc:
                    raise RuntimeError(f'symbolic worker exited without receipt: {proc.exitcode}') from exc
                if kind == 'error':
                    raise RuntimeError(f'symbolic worker failed: {result}')
                return result
            return {'query':query,'representative':representative,'status':'undecided',
                    'reason':'hard_timeout','levels':[],'proof_admitted':False,
                    'bounded_k':None,'domain_sha256':self.domain_identity(),
                    'comparison_contract':'canonical-exact-decimal-float3dp-list-bag-errors-v3' if canonical_contract else None,
                    'wall_s':budget_s}
        finally:
            parent.close()
            if proc.is_alive():
                proc.terminate()
            proc.join(0.2)
            if proc.is_alive():
                proc.kill()
                proc.join()

    def _search_inner(self, query, representative, budget_s, bounds=None, representative_pairs=(), augment=3, canonical_contract=False, progress=None):
        if query not in self.queries or representative not in self.queries:
            raise KeyError('unregistered query')
        if query == representative:
            raise ValueError('representative search requires two query identities')
        if not math.isfinite(budget_s):
            raise ValueError('finite remaining budget required')
        if budget_s <= 0:
            return {'status':'undecided','reason':'budget','levels':[],'proof_admitted':False,'bounded_k':None,
                 'augment':augment,'augment_rounds':0,'augment_extra_pairs':0,
                 'comparison_contract':'canonical-exact-decimal-float3dp-list-bag-errors-v3' if canonical_contract else None}
        if bounds is not None and (not bounds or any(type(k) is not int or k < 1 for k in bounds) or list(bounds)!=sorted(set(bounds))):
            raise ValueError('bounds must be a nonempty increasing positive schedule')
        start=time.perf_counter();deadline=start+budget_s
        receipt={'query':query,'representative':representative,'domain_sha256':self.domain_identity(),
                 'domain_revision':self.domain_revision,'schema_sha256':digest({'ddl':self.schema.ddl, 'tables':{n:asdict(t) for n,t in self.schema.tables.items()}}),
                 'query_sha256':digest(self.queries[query]),'representative_sha256':digest(self.queries[representative]),
                 'scope':'encoded_fragment_fixed_value_domain_both_executions_valid',
                 'encoding_sources':{p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in Path(__file__).parent.glob('*.py')},
                 'levels':[],'proof_admitted':False,'bounded_k':None,
                 'augment':augment,'augment_rounds':0,'augment_extra_pairs':0,
                 'comparison_contract':'canonical-exact-decimal-float3dp-list-bag-errors-v3' if canonical_contract else None}
        if query in self.unsupported or representative in self.unsupported:
            return {**receipt,'status':'unsupported','reason':'front_end'}
        try:
            schedule = bounds if bounds is not None else (2**i for i in itertools.count())
            for k in schedule:
                if time.perf_counter() >= deadline:
                    return {**receipt,'status':'undecided','reason':'budget'}
                lv,reused=self._level(k,deadline)
                if len(lv.enc.side) > self.max_side_constraints:
                    return {**receipt,'status':'undecided','reason':'resource_limit',
                            'side_constraints':len(lv.enc.side),'k':k,'wall_s':time.perf_counter()-start}
                if not (lv.ok(query) and lv.ok(representative)):
                    return {**receipt,'status':'unsupported','reason':'encoding','detail':dict(lv.unsup)}
                before,solve=lv.checks,lv.solve_s
                target=z3.And(lv.valid([query,representative]),lv.sep(query,representative))
                answer,model=lv.check(target)
                if answer==z3.sat:
                    # Preserve the target and every attained auxiliary gain.
                    keep=[target];remaining=list(representative_pairs)
                    rounds=0
                    for _ in range(augment):
                        more=[];pending=[]
                        for a,b in remaining:
                            if not (lv.ok(a) and lv.ok(b)):continue
                            term=z3.And(lv.valid([a,b]),lv.sep(a,b))
                            if z3.is_true(model.eval(term,model_completion=True)):keep.append(term)
                            else:more.append(term);pending.append((a,b))
                        if not more or time.perf_counter()>=deadline:break
                        extra,candidate=lv.check(z3.And(*keep,z3.Or(*more)))
                        if extra!=z3.sat:break
                        rounds+=1
                        model=candidate;remaining=pending
                    assert z3.is_true(model.eval(target,model_completion=True))
                    receipt['augment_rounds']=rounds
                    receipt['augment_extra_pairs']=max(0,len(keep)-1)
                admissible = False
                if canonical_contract and answer == z3.unsat:
                    # Exact output equality implies equality under typed
                    # decimal-exact / float-three-decimal normalization. Excluded errors or arbitrary order
                    # cannot be hidden inside a bounded certificate.
                    unsafe = z3.Or(lv.err[query], lv.err[representative],
                        *(z3.Not(lv.out[q].det) for q in (query, representative)
                          if lv.out[q].det is not None))
                    guard, _ = lv.check(unsafe)
                    admissible = guard == z3.unsat
                    if admissible:
                        receipt['bounded_k'] = k
                receipt['levels'].append({'k':k,'canonical_admissible':admissible,'encoding_reused':reused,'solver_calls':lv.checks-before,
                    'solve_s':lv.solve_s-solve,'answer':str(answer)})
                if admissible and progress is not None:
                    progress({**receipt, 'levels':list(receipt['levels']), 'status':'undecided',
                              'reason':'bounded_prefix', 'wall_s':time.perf_counter()-start})
                if answer==z3.sat:
                    db=lv.decode(model,self.schema)
                    assert not self.schema.violations(db),'decoded model violates declared constraints'
                    return {**receipt,'status':'candidate','k':k,'database':db,'wall_s':time.perf_counter()-start}
                if answer!=z3.unsat:
                    return {**receipt,'status':'undecided','reason':'solver_unknown'}
                if canonical_contract and not admissible:
                    return {**receipt,'status':'undecided','reason':'error_or_nondeterminism_scope_not_closed'}
            return {**receipt,'status':'domain_unsat','k':bounds[-1],'wall_s':time.perf_counter()-start}
        except (TimeoutError, MemoryError):
            self.levels.pop(k, None)
            return {**receipt,'status':'undecided','reason':'resource_or_deadline'}
        except Unsupported as e:
            self.levels.pop(k, None)
            return {**receipt,'status':'unsupported','reason':e.code}
