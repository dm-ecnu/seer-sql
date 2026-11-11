"""Class disjunctions over a capacity-growing Polygon encoding.

One solver per representative survives chunks, SAT feedback and deepening.
Each capacity has an activation literal: in particular, shallow FK clauses
must NOT constrain deeper databases. Existing row objects and Z3 DAG terms
are shared; old capacity outputs/clauses stay resident, never reconstructed.
The value domain and numeric certificate admission remain the frozen contract.
"""
from __future__ import annotations
import multiprocessing as mp
import time
from dataclasses import asdict
import z3
from .encode import Enc, _or
from .ir import Unsupported
from .symbolic import Level, est_cost
from .representative_search import digest


class ClassDepth(Level):
    def __init__(self, context, k, previous, deadline):
        self.context, self.k, self.dom = context, k, context.search.domain
        self.deadline, self.timeout_ms = deadline, context.search.timeout_ms
        search = context.search
        self.enc = Enc(search.schema, search.front, self.dom, k, search.dialect,
                       prefix='class_rows', previous=previous.enc if previous else None)
        self.enc.deadline = deadline
        self.out, self.err, self.unsup, self.irs, self._sep = {}, {}, {}, {}, {}
        self.checks, self.solve_s = 0, 0.
        self.solver = context.solver
        self.active = z3.FreshBool(f'capacity_{k}')
        self.synced = 0
        self.sync()

    def sync(self):
        for clause in self.enc.side[self.synced:]:
            self.solver.add(z3.Implies(self.active, clause))
        self.synced = len(self.enc.side)

    def extend(self, q):
        if q in self.out or q in self.unsup:
            return
        search = self.context.search
        if q in search.unsupported:
            self.unsup[q] = search.unsupported[q]
            return
        if est_cost(search.irs[q], self.k, search.schema) > search.max_cost:
            self.unsup[q] = 'encoding_too_large'
            return
        side = len(self.enc.side)
        memo = dict(self.enc.memo)
        self.enc.errs = []
        try:
            self.out[q] = self.enc.rel(search.irs[q])
            self.err[q] = _or(self.enc.errs)
            self.irs[q] = search.irs[q]
            self.sync()
        except Unsupported as exc:
            self.enc.side[side:] = []
            self.enc.memo = memo
            self.unsup[q] = exc.code
            if exc.code == 'encoding_timeout':
                raise TimeoutError(str(exc))

    def check(self, extra):
        left = min(self.timeout_ms, int(1000*(self.deadline-time.perf_counter())))
        if left <= 0:
            self.last_unknown_reason = 'deadline'
            return z3.unknown, None
        self.solver.set(timeout=max(1, left))
        started = time.perf_counter()
        self.solver.push()
        try:
            self.solver.add(self.active, extra)
            # Explicitly deactivate all other capacities (old FK scopes differ).
            self.solver.add([z3.Not(v.active) for v in self.context.levels.values() if v is not self])
            answer = self.solver.check()
            self.last_unknown_reason = self.solver.reason_unknown() if answer == z3.unknown else None
            model = self.solver.model() if answer == z3.sat else None
            return answer, model
        finally:
            self.solver.pop()
            self.checks += 1
            self.solve_s += time.perf_counter()-started


class ClassContext:
    def __init__(self, search, representative):
        self.search, self.representative = search, representative
        self.solver = z3.Solver()
        self.solver.set(random_seed=search.seed)
        self.levels = {}
        self.schema_sha = digest({'ddl':search.schema.ddl,
                                 'tables':{n:asdict(t) for n,t in search.schema.tables.items()}})

    def check(self, members, k, budget_s, representative_pairs=()):
        started = time.perf_counter(); deadline = started + budget_s
        rep = self.representative
        receipt = dict(status='undecided', representative=rep, members=list(members), k=k,
            domain_sha256=self.search.domain_identity(), schema_sha256=self.schema_sha,
            scope='encoded_fragment_fixed_value_domain_both_executions_valid',
            comparison_contract='canonical-exact-decimal-float3dp-list-bag-errors-v3',
            proof_admitted=False, solver_calls=0, class_disjunction=True)
        # Reduce chunks before encoding, using the same Polygon cost estimator.
        cost = sum(est_cost(self.search.irs[q], k, self.search.schema)
                   for q in [rep, *members] if q in self.search.irs)
        if cost > self.search.max_cost and len(members) > 1:
            return dict(receipt,status='reduce_chunk',estimated_cost=cost)
        try:
            reused = k in self.levels
            if not reused:
                previous = self.levels[max(self.levels)] if self.levels else None
                if previous is not None and k < previous.k:
                    raise ValueError('missing shallow capacity cannot be rebuilt')
                self.levels[k] = ClassDepth(self,k,previous,deadline)
            lv = self.levels[k]; lv.deadline = lv.enc.deadline = deadline
            before, solve = lv.checks, lv.solve_s
            for q in [rep, *members]:
                lv.extend(q)
            receipt['unsupported'] = {q:lv.unsup[q] for q in [rep,*members] if q in lv.unsup}
            if not lv.ok(rep):
                return dict(receipt,status='unsupported',reason='representative_encoding')
            live = [q for q in members if lv.ok(q)]
            if not live:
                return dict(receipt,status='unsupported',reason='member_encoding')
            if len(lv.enc.side) > self.search.max_side_constraints:
                return dict(receipt,status='undecided',reason='side_constraint_guard')
            targets = {q:z3.And(lv.valid([rep,q]),lv.sep(rep,q)) for q in live}
            receipt['encoded_members'] = live
            receipt['encoding_reused'] = reused
            receipt['capacity_count'] = len(self.levels)
            receipt['encoding_s'] = time.perf_counter()-started
            answer, model = lv.check(z3.Or(*targets.values()))
            if representative_pairs:
                receipt['disjunction_result'] = str(answer)
                receipt['solver_unknown_reason'] = lv.last_unknown_reason
            if answer == z3.sat:
                if representative_pairs:
                    from .class_objective import extend_model,complete_order_choices
                    extra_started=time.perf_counter();side_before=len(lv.enc.side)
                    receipt.update(objective_rounds=0,objective_extra_separations=0,objective_checks=[])
                    try:
                        # Share each representative's cached encoding, and only
                        # pay for it after the original block has a SAT model.
                        for q in dict.fromkeys(q for pair in representative_pairs for q in pair):
                            if len(lv.enc.side)>self.search.max_side_constraints:
                                raise Unsupported('objective_side_constraint_guard')
                            lv.extend(q)
                        if len(lv.enc.side)>self.search.max_side_constraints:
                            raise Unsupported('objective_side_constraint_guard')
                        complete_order_choices(lv,model,lv.enc.side[side_before:])
                        objective_targets={(a,b):z3.And(lv.valid([a,b]),lv.sep(a,b))
                            for a,b in representative_pairs if lv.ok(a) and lv.ok(b)}
                        receipt['objective_encoding_s']=time.perf_counter()-extra_started
                        model, objective = extend_model(lv,model,targets,objective_targets)
                        receipt.update(objective)
                    except (Unsupported,TimeoutError,MemoryError,RecursionError) as exc:
                        # The original SAT row assignment remains a usable
                        # candidate even when optional encoding cannot finish.
                        receipt['objective_skip_reason']=getattr(exc,'code',type(exc).__name__)
                    finally:
                        receipt.setdefault('objective_encoding_s',time.perf_counter()-extra_started)
                        receipt['encoding_s']+=receipt['objective_encoding_s']
                database = lv.decode(model,self.search.schema)
                assert not self.search.schema.violations(database), 'class split violates Sigma'
                receipt.update(status='candidate',database=database,
                    differing=[q for q,t in targets.items() if z3.is_true(model.eval(t,model_completion=True))])
                assert receipt['differing'], 'SAT class disjunction has no differing member'
            elif answer == z3.unsat:
                unsafe = z3.Or(*( [lv.err[q] for q in [rep,*live]] +
                    [z3.Not(lv.out[q].det) for q in [rep,*live] if lv.out[q].det is not None]))
                guard,_ = lv.check(unsafe)
                if representative_pairs:
                    receipt['safety_result'] = str(guard)
                    receipt['safety_unknown_reason'] = lv.last_unknown_reason
                receipt.update(status='domain_unsat' if guard == z3.unsat else 'undecided',
                    reason='bounded' if guard == z3.unsat else 'error_or_nondeterminism_scope_not_closed',
                    bounded_k=k if guard == z3.unsat else None)
            else:
                receipt['reason'] = 'solver_unknown'
            receipt.update(solver_calls=lv.checks-before, solve_s=lv.solve_s-solve,
                           wall_s=time.perf_counter()-started)
            return receipt
        except Unsupported as exc:
            return dict(receipt,status='unsupported',reason=exc.code,wall_s=time.perf_counter()-started)
        except (TimeoutError, MemoryError, RecursionError) as exc:
            return dict(receipt,reason=type(exc).__name__,wall_s=time.perf_counter()-started)


def _worker(search, rep, pipe):
    try:
        context = ClassContext(search,rep)
        while True:
            command = pipe.recv()
            if command is None:
                return
            try:
                pipe.send(('ok',context.check(*command)))
            except BaseException as exc:
                pipe.send(('error',f'{type(exc).__name__}: {exc}'))
                return
    except (EOFError,BrokenPipeError):
        pass
    finally:
        pipe.close()


class ClassWorker:
    """Hard deadline around encoding and Z3; no restart of a timed-out class."""
    def __init__(self, search, rep):
        ctx=mp.get_context('fork'); self.pipe, child=ctx.Pipe()
        self.proc=ctx.Process(target=_worker,args=(search,rep,child),daemon=True)
        self.proc.start(); child.close(); self.closed=False

    def check(self,members,k,budget_s,representative_pairs=()):
        if self.closed:
            raise RuntimeError('timed-out class contexts cannot restart')
        command=(members,k,max(0.,budget_s-.1))
        if representative_pairs:command+= (representative_pairs,)
        self.pipe.send(command)
        if not self.pipe.poll(max(0.,budget_s)):
            self.close()
            return dict(status='undecided',reason='class_hard_timeout',k=k,members=list(members),solver_calls=0)
        try:
            kind,value=self.pipe.recv()
        except EOFError:
            self.close()
            return dict(status='undecided',reason='class_worker_exited',k=k,members=list(members),solver_calls=0)
        if kind!='ok':
            self.close()
            raise RuntimeError(value)
        return value

    def close(self):
        if self.closed:return
        self.closed=True
        if self.proc.is_alive():
            try:self.pipe.send(None)
            except (OSError,BrokenPipeError):pass
            self.proc.join(.1)
        if self.proc.is_alive():self.proc.terminate();self.proc.join(.2)
        if self.proc.is_alive():self.proc.kill();self.proc.join()
        self.pipe.close()
