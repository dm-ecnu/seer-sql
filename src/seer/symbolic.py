"""Symbolic tier: one bounded symbolic database (bound k) shared by all still-unsplit queries of a batch.

For each round the solver is asked for a Σ-valid database on which some class of the current partition is not
uniform. Representative augmentation preserves differences among predicted-block representatives; the optional
max_pairs policy instead maximizes explicit valid pair predicates within its budget. The model is decoded into a concrete
database, and handed back to the caller, who executes it on the real engine. Rounds of one level share the
encoding; each round's goal lives inside push/pop. UNSAT of the round goal at level k means: no database with at
most k tuples per table over the value domain D separates any pair of any class, in the fragment encoded.
"""
from __future__ import annotations

import datetime as dt
import decimal
import time
from fractions import Fraction

import z3

from . import ir, joins
from .encode import Enc, F_, T, _and, _count, _nseq, _not, _or
from .funcs import date_of
from .ir import BOOL, DATE, FLOAT, INT, NULL, STR, TS, Unsupported, is_dec, is_num


def _kind(t):
    if is_num(t):
        return "num"
    return t


def _row_eq(u, v, tu, tv):
    out = []
    for a, b, x, y in zip(u, v, tu, tv):
        if NULL in (x, y) or _kind(x) == _kind(y):
            if NULL in (x, y):
                out.append(_and([a[1], b[1]]))
            else:
                out.append(_nseq(a, b))
        else:
            out.append(_and([a[1], b[1]]))
    return _and(out)


def est_rows(r, k, S=None):
    """Upper bound on the number of encoded slots of a relation (for the size guard); with the schema, join blocks
    are sized by their key-lookup plan (joins.plan), as encoded."""
    op, a = r.op, r.args
    if S is not None and (joins.is_block_join(r) or (op == "filter" and joins.is_block_join(a[0]))):
        leaves, conds = joins.flatten(r if op == "join" else a[0])
        if op == "filter":
            joins.add_cond(conds, a[1], 0, r.arity)
        _, n = joins.plan(leaves, conds, [est_rows(x, k, S) for x, _ in leaves],
                          [joins.leaf_keys(x, S) for x, _ in leaves])
        return n
    if op == "scan":
        return k
    if op == "values":
        return len(a[0])
    if op in ("filter", "project", "distinct", "distinct_on", "window", "sort"):
        return est_rows(a[0], k, S)
    if op == "agg":
        return est_rows(a[0], k, S) if a[1] else 1
    if op == "join":
        lr, rr = est_rows(a[1], k, S), est_rows(a[2], k, S)
        kind = a[0][4:] if a[0].startswith("lat_") else a[0]
        if kind in ("semi", "anti"):
            return lr
        return lr * rr + (lr if kind in ("left", "full") else 0) + (rr if kind in ("right", "full") else 0)
    if op == "setop":
        return est_rows(a[2], k, S) + (est_rows(a[3], k, S) if a[0] == "union" else 0)
    return k


def est_cost(r, k, S=None):
    """Rough term-count estimate: quadratic operators dominate; correlated subqueries multiply by outer rows."""
    total = 0
    inner = set()                  # join nodes inside a join block: encoded as part of the block, not on their own
    for x in ir.walk_rel(r):
        if S is not None and (joins.is_block_join(x) or (x.op == "filter" and joins.is_block_join(x.args[0]))):
            stack = [x.args[1], x.args[2]] if x.op == "join" else [x.args[0]]
            while stack:
                y = stack.pop()
                if joins.is_block_join(y):
                    inner.add(id(y))
                    stack += [y.args[1], y.args[2]]
    for x in ir.walk_rel(r):
        if id(x) in inner:
            continue
        n = est_rows(x, k, S)
        if x.op in ("agg", "distinct", "distinct_on", "window", "sort", "setop"):
            total += n * n
        else:
            total += n
        exprs, _ = ir._rel_exprs(x)
        for e in exprs:
            for y in ir._walk_e(e):
                if y.op == "sub":
                    sub = y.args[1]
                    total += n * est_rows(sub, k, S) * (2 if ir.free_depth(sub) else 0)
    return total


class Level:
    def __init__(self, schema, front, dom, irs, k, dialect, timeout_ms=20000, max_cost=400000, seed=0, deadline=None, per_query_s=None, share_query_encoding=True, encode_sigma=True):
        self.k, self.irs, self.dom = k, irs, dom
        self.deadline, self.timeout_ms = deadline, int(timeout_ms)
        t0 = time.perf_counter()
        self.enc = Enc(schema, front, dom, k, dialect, prefix=f"k{k}", encode_sigma=encode_sigma)
        self.enc.deadline = deadline
        self.out, self.err, self.unsup = {}, {}, {}
        semantic_memo_keys = set()
        for q, r in irs.items():
            if not share_query_encoding:
                removed = len(self.enc.memo) - len(semantic_memo_keys)
                self.enc.stats['cross_query_memo_entries_discarded'] = self.enc.stats.get('cross_query_memo_entries_discarded', 0) + removed
                self.enc.memo = {key: value for key, value in self.enc.memo.items() if key in semantic_memo_keys}
                self.enc.stats['query_memo_resets'] = self.enc.stats.get('query_memo_resets', 0) + 1
            c = est_cost(r, k, schema)
            if c > max_cost:
                self.unsup[q] = f"encoding_too_large(k={k}, cost~{c})"
                continue
            self.enc.errs = []
            qd = None if per_query_s is None else time.perf_counter() + per_query_s
            self.enc.deadline = qd if deadline is None else (deadline if qd is None else min(deadline, qd))
            try:
                self.out[q] = self.enc.rel(r)
            except Unsupported as u:
                self.unsup[q] = f"unsupported:{u.code}"
                continue
            except (z3.Z3Exception, RecursionError) as ex:
                self.unsup[q] = f"encode_error:{type(ex).__name__}"
                continue
            finally:
                if not share_query_encoding:
                    # Memoization is also what couples nondeterministic LIMIT
                    # choices, including the identities of correlated inputs.
                    # Retain the complete dependency closure of such a query;
                    # only deterministic cross-query operator reuse is ablated.
                    from .ir import walk_rel
                    if any(node.op == 'sort' and any(x is not None for x in node.args[2:]) for node in walk_rel(r)):
                        semantic_memo_keys.update(self.enc.memo)
                    self.enc.stats['semantic_memo_entries_peak'] = max(self.enc.stats.get('semantic_memo_entries_peak', 0), len(semantic_memo_keys))
            self.err[q] = _or(self.enc.errs)
        self.enc.deadline = deadline
        self.solver = z3.Solver()
        self.solver.set("timeout", int(timeout_ms))
        self.solver.set("random_seed", seed)
        self.solver.add(self.enc.side)
        self.encode_s = time.perf_counter() - t0
        self._sep = {}
        self.checks = 0
        self.solve_s = 0.0

    def ok(self, q):
        return q in self.out

    def sep(self, a, b):
        key = (a, b) if a < b else (b, a)
        if key in self._sep:
            return self._sep[key]
        A, B = self.out[a], self.out[b]
        ea = [e for e, _ in A.rows]
        eb = [e for e, _ in B.rows]
        if len(A.types) != len(B.types):
            s = _or(ea + eb)
        else:
            sa, sb = _count(ea), _count(eb)
            terms = [sa != sb]
            for e, v in A.rows:
                self._tick()
                ma = _count([_and([e2, _row_eq(v2, v, A.types, A.types)]) for e2, v2 in A.rows])
                mb = _count([_and([e2, _row_eq(v2, v, B.types, A.types)]) for e2, v2 in B.rows])
                terms.append(_and([e, ma != mb]))
            bag = _or(terms)
            s = bag
            if A.pos is not None and B.pos is not None:
                lst = []
                for (e1, v1), p1 in zip(A.rows, A.pos):
                    self._tick()
                    for (e2, v2), p2 in zip(B.rows, B.pos):
                        lst.append(_and([e1, e2, p1 == p2, _not(_row_eq(v1, v2, A.types, B.types))]))
                s = _or([bag, _and([A.det, B.det, _or(lst)])])
        self._sep[key] = s
        return s

    def _tick(self):
        if self.deadline is not None and time.perf_counter() > self.deadline:
            raise TimeoutError("budget exhausted while building separation terms")

    def valid(self, cls):
        return _and([_not(self.err[q]) for q in cls])

    def check(self, extra):
        # A scoped push may spend unbounded time internalizing a large prior
        # level before Z3's check timeout is installed. The optional check
        # assumption is temporary and preserves the incremental base solver.
        left_ms = self.timeout_ms
        if self.deadline is not None:
            left_ms = min(left_ms, int((self.deadline - time.perf_counter()) * 1000))
            if left_ms <= 0:
                return z3.unknown, None
        self.solver.set("timeout", max(1, left_ms))
        # The surrounding search worker has a process deadline, including
        # encoding and native calls that ignore Z3's own timeout.
        t0 = time.perf_counter()
        res = self.solver.check(extra)
        self.solve_s += time.perf_counter() - t0
        self.checks += 1
        m = self.solver.model() if res == z3.sat else None
        return res, m

    def goal_pairs(self,pairs,policy='all_pairs'):
        from .goal_graph import select_pairs
        return select_pairs(self,pairs,policy)

    def find(self, classes, augment=3, budget_s=None, selection="representatives"):
        if selection == "max_pairs":
            return self.find_max_pairs(classes, budget_s)
        if selection != "representatives":
            raise ValueError("unknown symbolic selection: " + selection)
        return self._find_representatives(classes, augment, budget_s)

    def find_max_pairs(self, classes, budget_s=None):
        """Maximize valid, current-class pair coverage within the existing solver budget.

        SAT models improve a retained lower bound; UNSAT thresholds tighten its
        upper bound. UNKNOWN or deadline returns the best feasible model. The
        optimal flag applies only to this level and its encoded valid-class goal.
        """
        started = time.perf_counter()
        original_deadline = self.deadline
        local_deadline = None if budget_s is None else started + max(0.0, budget_s)
        if local_deadline is not None:
            self.deadline = local_deadline if original_deadline is None else min(original_deadline, local_deadline)
        upper = sum(len(c) * (len(c) - 1) // 2 for c in classes)
        info = {"policy": "max_pairs", "lower": 0, "upper": upper, "optimal": False, "stop": "initial"}
        self.selection_info = info
        best = pred = None
        def gain(model, terms):
            return sum(z3.is_true(model.eval(term, model_completion=True)) for term, _ in terms)
        def finish(stop):
            info["stop"] = stop
            info["wall_s"] = time.perf_counter() - started
        try:
            goals = [_and([self.valid(c), _or([self.sep(c[0], q) for q in c[1:]])]) for c in classes]
            res, best = self.check(_or(goals))
            if res == z3.unknown:
                finish("unknown")
                return "unknown", None, None
            if res == z3.sat:
                pred = [[list(c)] for c in classes]
                pred = self._predicted(best, classes)
            # Use representative comparisons only to seed a feasible model.
            # Their UNSAT result is not exhaustive when mixed ordering makes
            # result comparison nontransitive. The full objective uses every
            # valid pair predicate and also supplies the fallback SAT goal.
            low, high = (1 if best is not None else 0), upper
            info["lower"] = low
            terms = []
            observed = 0
            for c in classes:
                valid = self.valid(c)
                for i, a in enumerate(c):
                    for b in c[:i]:
                        self._tick()
                        term = _and([valid, self.sep(a, b)])
                        terms.append((term, 1))
                        if best is not None:
                            observed += z3.is_true(best.eval(term, model_completion=True))
                        info["lower"] = max(low, observed)
            if best is None:
                res, best = self.check(_or([term for term, _ in terms]))
                if res != z3.sat:
                    info.update(upper=0 if res == z3.unsat else upper, optimal=res == z3.unsat)
                    finish("unsat" if res == z3.unsat else "unknown")
                    return ("unsat" if res == z3.unsat else "unknown"), None, None
                pred = [[list(c)] for c in classes]
                pred = self._predicted(best, classes)
                observed = gain(best, terms)
            low = observed
            info["lower"] = low
            while low < high:
                self._tick()
                threshold = (low + high + 1) // 2
                res, candidate = self.check(z3.PbGe(terms, threshold))
                if res == z3.unknown:
                    finish("unknown")
                    return "sat", best, pred
                if res == z3.unsat:
                    high = threshold - 1
                else:
                    candidate_pred = self._predicted(candidate, classes)
                    value = gain(candidate, terms)
                    assert value >= threshold, (value, threshold)
                    best, pred, low = candidate, candidate_pred, value
                info.update(lower=low, upper=high)
            info["optimal"] = True
            finish("optimal")
            return "sat", best, pred
        except TimeoutError:
            finish("budget")
            return ("sat", best, pred) if best is not None and pred is not None else ("unknown", None, None)
        finally:
            self.deadline = original_deadline

    def _find_representatives(self, classes, augment=3, budget_s=None):
        """classes: list of lists of query ids (only encodable members). Returns (status, model, predicted)."""
        goals = []
        for C in classes:
            rep = C[0]
            goals.append(_and([self.valid(C), _or([self.sep(rep, q) for q in C[1:]])]))
        res, m = self.check(_or(goals))
        if res != z3.sat:
            return ("unsat" if res == z3.unsat else "unknown"), None, None
        pred = self._predicted(m, classes)
        t0 = time.perf_counter()
        for _ in range(augment):
            if budget_s is not None and time.perf_counter() - t0 > budget_s:
                break
            keep, more = [], []
            for ci, C in enumerate(classes):
                blocks = pred[ci]
                if len(blocks) > 1:
                    keep.append(self.valid(C))
                    reps = [b[0] for b in blocks]
                    for i in range(len(reps)):
                        for j in range(i):
                            keep.append(self.sep(reps[i], reps[j]))
                for b in blocks:
                    for q in b[1:]:
                        more.append(_and([self.valid(C), self.sep(b[0], q)]))
            if not more:
                break
            res2, m2 = self.check(_and(keep + [_or(more)]))
            if res2 != z3.sat:
                break
            m = m2
            pred = self._predicted(m, classes)
        return "sat", m, pred

    def _predicted(self, m, classes):
        out = []
        for C in classes:
            if not z3.is_true(m.eval(self.valid(C), model_completion=True)):
                out.append([list(C)])
                continue
            blocks = []
            for q in C:
                for b in blocks:
                    if not z3.is_true(m.eval(self.sep(b[0], q), model_completion=True)):
                        b.append(q)
                        break
                else:
                    blocks.append([q])
            out.append(blocks)
        return out

    def decode(self, m, schema):
        db = {}
        for t in schema.order:
            tb = schema.tables[t]
            rows = []
            for ex, cells in self.enc.vars[t]:
                if not z3.is_true(m.eval(ex, model_completion=True)):
                    continue
                row = {}
                for c, (_, ty), (var, nul) in zip(tb.columns, self.enc.tables[t], cells):
                    if not z3.is_false(nul) and z3.is_true(m.eval(nul, model_completion=True)):
                        row[c.name] = None
                        continue
                    row[c.name] = self._val(m.eval(var, model_completion=True), ty)
                rows.append(row)
            db[t] = rows
        return db

    def _val(self, v, ty):
        if ty == BOOL:
            return z3.is_true(v)
        if ty == INT:
            return v.as_long()
        if is_dec(ty):
            s = ir.dec_scale(ty)
            return decimal.Decimal(v.as_long()).scaleb(-s)
        if ty == FLOAT:
            f = Fraction(v.numerator_as_long(), v.denominator_as_long()) if z3.is_rational_value(v) \
                else Fraction(str(v.approx(12)).rstrip("?"))
            return float(f)
        if ty == STR:
            return self.dom.strings[v.as_long()]
        if ty == DATE:
            return date_of(v.as_long())
        if ty == TS:
            return dt.datetime(1970, 1, 1) + dt.timedelta(seconds=v.as_long())
        raise ValueError(ty)
