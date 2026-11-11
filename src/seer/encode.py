"""Bounded symbolic database and operator encodings (in-process z3, typed values).

Lineage. The encoding follows Polygon (PLDI'25; vendored unmodified at vendor/polygon, see vendor/polygon/UPSTREAM.md)
operator by operator and keeps its design: a table of bound k is k tuples with an existence bit each (Polygon's
Deleted), every cell is a (value, null) pair (SMTCell / SMTNull), predicates are three-valued, bag equality is
equality of multiplicities (environment.o1_eq_o2), grouping uses the representative-tuple encoding with per-tuple
membership flags (formulas/group_by.py, visitors/group_expression_encoder.py), DISTINCT keeps the first occurrence
(formulas/distinct.py), and VeriEQL/Polygon's LEFT/FULL join padding. Differences, each listed with its reason in
vendor/polygon/PATCHES.md:
  * terms are built directly in z3 (no SMT-LIB text, no subprocess), so the solver keeps state across checks
    (push/pop) and every operator output is a term over the base cells, hash-consed by z3;
  * typed values instead of "everything is Int": INT -> Int, DECIMAL(p,s) -> scaled Int (exact), FLOAT and
    derived decimals -> Real, strings -> index into a sorted finite pool (so <, LIKE, SUBSTRING, UPPER, ... are
    exact over the pool), dates -> day numbers in a bounded range (EXTRACT, + INTERVAL month are exact there);
  * fixes: IN / NOT IN / ANY / ALL with three-valued semantics, scalar subqueries that return 0 rows (NULL) or
    more than one row (runtime error), HAVING without GROUP BY, RIGHT JOIN column order, FULL JOIN, ORDER BY with
    ties, composite foreign keys (MATCH SIMPLE), UNIQUE with NULLs, CHECK under three-valued logic;
  * new operators: INTERSECT / EXCEPT (set and ALL), semi / anti join, window functions (ROW_NUMBER, RANK,
    DENSE_RANK, aggregate OVER), ORDER BY / LIMIT / OFFSET with list semantics.
  * no output caps: Polygon under-approximates operator outputs with choice vectors of size k; here each operator
    keeps all candidate slots, so a verdict at bound k quantifies over all databases with <= k tuples per table.

Runtime errors (scalar subquery with >1 row, division by zero where the dialect raises or yields inf, nondeterminism
such as LIMIT across ties) are collected as conditions in Enc.errs; a witness database must make all of them false.
"""
from __future__ import annotations

import datetime as dt
import time
import decimal
from fractions import Fraction

import z3

from . import ir, joins
from .funcs import STR_MAPS, STR_NUMS, add_months, date_of, day_of, like
from .ir import BOOL, DATE, FLOAT, INT, NULL, STR, TS, E, R, Unsupported, is_dec, is_num

T, F_ = z3.BoolVal(True), z3.BoolVal(False)


def _and(xs):
    xs = [x for x in xs if not z3.is_true(x)]
    if any(z3.is_false(x) for x in xs):
        return F_
    if not xs:
        return T
    return xs[0] if len(xs) == 1 else z3.And(xs)


def _or(xs):
    xs = [x for x in xs if not z3.is_false(x)]
    if any(z3.is_true(x) for x in xs):
        return T
    if not xs:
        return F_
    return xs[0] if len(xs) == 1 else z3.Or(xs)


def _not(x):
    if z3.is_true(x):
        return F_
    if z3.is_false(x):
        return T
    return z3.Not(x)


def _ite(c, a, b):
    if z3.is_true(c):
        return a
    if z3.is_false(c):
        return b
    return z3.If(c, a, b)


def _sum(xs, zero):
    xs = list(xs)
    if not xs:
        return zero
    return xs[0] if len(xs) == 1 else z3.Sum(xs)


def _count(bs):
    return _sum([_ite(b, z3.IntVal(1), z3.IntVal(0)) for b in bs if not z3.is_false(b)], z3.IntVal(0))


class Rel:
    """Encoded relation: rows = [(exists, (val, ...))], val = (term, isnull). For a top-level sorted relation also
    pos (output position term per row), det (no ties among existing rows), limited."""
    __slots__ = ("rows", "pos", "det", "limited", "types")

    def __init__(self, rows, types, pos=None, det=None, limited=False):
        self.rows, self.types, self.pos, self.det, self.limited = rows, types, pos, det, limited


# ---------------------------------------------------------------------- value domain
class Domain:
    """The finite value domain D for strings and the date range. Strings are encoded by their index in the sorted
    pool, so order comparisons on indices are exact (binary collation)."""

    def __init__(self, strings, years=(1990, 2000), max_pool=1500):
        self.strings = sorted(set(strings))
        if len(self.strings) > max_pool:
            self.strings = self.strings[:max_pool]
        self.index = {s: i for i, s in enumerate(self.strings)}
        self.ylo, self.yhi = years
        self.dlo = day_of(dt.date(self.ylo, 1, 1))
        self.dhi = day_of(dt.date(self.yhi, 12, 31))
        self.month_starts = [day_of(dt.date(y, m, 1)) for y in range(self.ylo, self.yhi + 1) for m in range(1, 13)]

    @staticmethod
    def build(rels, extra_strings=(), checks_strings=(), max_pool=1500, closure_rounds=2):
        inputs = DomainInputs(extra_strings, checks_strings, max_pool, closure_rounds)
        inputs.add(rels)
        return inputs.build()

    def code(self, s):
        return self.index.get(s)


class DomainInputs:
    """Monotone facts from incoming IRs; materialize the unchanged domain policy.

    Facts are collected once per incoming relation. Domain closure is recomputed
    only when the union of string/date/map facts changes, so transformations from
    older queries still apply to literals introduced by later queries.
    """
    def __init__(self, extra_strings=(), checks_strings=(), max_pool=1500, closure_rounds=2):
        self.lits = set(extra_strings) | set(checks_strings)
        self.years, self.maps = set(), set()
        self.max_pool, self.closure_rounds = max_pool, closure_rounds
        self.relation_roots_seen = self.expression_nodes_seen = self.materializations = 0
        self._domain, self._dirty = None, True

    def add(self, rels):
        lits, years, maps = self.lits, self.years, self.maps
        before = (len(lits), len(years), len(maps))
        for r in rels:
            self.relation_roots_seen += 1
            for e in ir.walk_exprs(r):
                self.expression_nodes_seen += 1
                if e.op == "lit":
                    v = e.args[0]
                    if e.ty == STR and isinstance(v, str):
                        lits.add(v)
                    elif e.ty in (DATE, TS) and v is not None:
                        years.add(v.year)
                elif e.op == "fn" and e.args[0] == "like":
                    from .funcs import like_examples
                    lits |= like_examples(e.args[2].args[0])
                elif e.op == "fn" and e.args[0] == "strmap":
                    maps.add(e.args[2].args[0])
                    for p in e.args[2].args[0][1:]:
                        if isinstance(p, str):
                            lits.add(p)
        changed = before != (len(lits), len(years), len(maps))
        self._dirty |= changed
        return changed

    def build(self):
        if self._domain is not None and not self._dirty:
            return self._domain
        lits, years, maps = set(self.lits), self.years, self.maps
        max_pool, closure_rounds = self.max_pool, self.closure_rounds
        lits |= {"", "a", "b", "zz"}
        # neighbours: a string just above each literal (strict gaps for < / >)
        lits |= {s + "a" for s in list(lits) if len(s) < 60}
        pool = set(lits)
        for _ in range(closure_rounds):
            new = set()
            for name, *ps in maps:
                f = STR_MAPS[name]
                for s in pool:
                    try:
                        v = f(s, *ps)
                    except Exception:
                        v = None
                    if v is not None and v not in pool:
                        new.add(v)
            if not new or len(pool) + len(new) > max_pool:
                pool |= set(sorted(new)[:max(0, max_pool - len(pool))])
                break
            pool |= new
        if years:
            # one year of slack on each side, inside the calendar Python and every engine can represent
            yr = (max(dt.MINYEAR, min(years) - 1), min(dt.MAXYEAR, max(years) + 1))
        else:
            yr = (1990, 2000)
        self._domain = Domain(pool, yr, max_pool)
        self._dirty = False
        self.materializations += 1
        return self._domain


def schema_strings(S):
    """String literals of the schema's CHECK constraints: they must be in the pool for Σ to be encodable."""
    from sqlglot import exp
    out = set()
    for tb in S.tables.values():
        for ch in tb.checks:
            for lit in ch.expr.find_all(exp.Literal):
                if lit.is_string:
                    out.add(lit.this)
    return out


# ---------------------------------------------------------------------- the encoder
class Enc:
    """Encodes a batch of IR queries over one bounded symbolic database (bound k per table)."""

    def __init__(self, schema, front, dom: Domain, k, dialect="duckdb", prefix="s", encode_sigma=True, previous=None):
        self.S, self.front, self.dom, self.k, self.dialect = schema, front, dom, k, dialect
        self.tables = front.tables
        self.encode_sigma = bool(encode_sigma)
        self.side = []            # domain + Σ constraints (hard)
        self.errs = []            # runtime-error conditions of the query currently being encoded
        self.guard = T
        self.deadline = None      # perf_counter() deadline; encoding past it raises Unsupported(encoding_timeout)
        self.memo = {}
        self._keep = []
        self.base = {}
        self.vars = {}            # table -> [(exists var, [(value var/term, null var)])]
        self.pfx = prefix
        self.stats = {"memo_hits": 0, "memo_miss": 0}
        self._previous_capacity = previous
        self._mk_base()

    # .................................................................. base tables and Σ
    def _k(self, t):
        return self.k[t] if isinstance(self.k, dict) else self.k

    def _mk_base(self):
        if self._previous_capacity is not None:
            self.side.extend(self._previous_capacity.base_side)
        for t in self.S.order:
            tb = self.S.tables[t]
            previous = self._previous_capacity
            rows = list(previous.base[t]) if previous is not None else []
            raw = list(previous.vars[t]) if previous is not None else []
            k = self._k(t)
            assert k >= len(rows), "capacity must grow monotonically"
            for i in range(len(rows), k):
                ex = z3.Bool(f"{self.pfx}_{t}_{i}_e")
                vals, rv = [], []
                for c, (_, ty) in zip(tb.columns, self.tables[t]):
                    nm = f"{self.pfx}_{t}_{i}_{c.name}"
                    nul = z3.Bool(nm + "_n") if c.nullable or not self.encode_sigma else F_
                    v, var = self._cell(nm, ty, c)
                    vals.append((v, nul))
                    rv.append((var, nul))
                rows.append((ex, tuple(vals)))
                raw.append((ex, rv))
            for i in range(k - 1):        # symmetry breaking: existing tuples form a prefix
                self.side.append(z3.Implies(rows[i + 1][0], rows[i][0]))
            self.base[t] = rows
            self.vars[t] = raw
        self.base_side = list(self.side)
        if self.encode_sigma:
            self._sigma()

    def _cell(self, nm, ty, c):
        if ty == INT:
            x = z3.Int(nm)
            self.side.append(z3.And(x >= -(2 ** 31), x < 2 ** 31))
            return x, x
        if is_dec(ty):
            s = ir.dec_scale(ty)
            p = (c.param or (18, s))[0]
            x = z3.Int(nm)
            lim = 10 ** min(p, 18)
            self.side.append(z3.And(x > -lim, x < lim))
            return z3.ToReal(x) / (10 ** s) if s else z3.ToReal(x), x
        if ty == FLOAT:
            x = z3.Real(nm)
            return x, x
        if ty == STR:
            x = z3.Int(nm)
            from seer2.pools import _strlen
            ml = _strlen(c.raw)
            allowed = [i for i, s in enumerate(self.dom.strings) if ml is None or len(s) <= ml]
            if len(allowed) == len(self.dom.strings):
                self.side.append(z3.And(x >= 0, x < len(self.dom.strings)))
            else:
                self.side.append(_or([x == i for i in allowed]))
            return x, x
        if ty == DATE:
            x = z3.Int(nm)
            self.side.append(z3.And(x >= self.dom.dlo, x <= self.dom.dhi))
            return x, x
        if ty == TS:
            x = z3.Int(nm)
            return x, x
        if ty == BOOL:
            x = z3.Bool(nm)
            return x, x
        raise Unsupported("column_type", ty)

    def _sigma(self):
        S = self.S
        for t in S.order:
            tb = S.tables[t]
            rows = self.base[t]
            idx = {c.name: j for j, c in enumerate(tb.columns)}
            keys = ([tb.pk] if tb.pk else []) + list(tb.uniques)
            for key in keys:
                js = [idx[c] for c in key]
                for a in range(len(rows)):
                    for b in range(a):
                        ea, va = rows[a]
                        eb, vb = rows[b]
                        same = _and([_and([_not(va[j][1]), _not(vb[j][1]), va[j][0] == vb[j][0]]) for j in js])
                        self.side.append(_not(_and([ea, eb, same])))
            for fk in tb.fks:
                prow = self.base[fk.ref]
                pidx = {c.name: j for j, c in enumerate(S.tables[fk.ref].columns)}
                cj = [idx[c] for c in fk.cols]
                pj = [pidx[c] for c in fk.ref_cols]
                if any(self.tables[t][j][1] != self.tables[fk.ref][q][1] for j, q in zip(cj, pj)):
                    # codes of different types live in different value spaces (string index vs integer value):
                    # equality of codes is not equality of values, so the inclusion is not encodable exactly
                    raise Unsupported("check_constraint", f"inclusion {t}{tuple(fk.cols)} -> {fk.ref} across types")
                for e, v in rows:
                    anynull = _or([v[j][1] for j in cj])
                    match = _or([_and([pe] + [_and([_not(pv[q][1]), pv[q][0] == v[j][0]]) for j, q in zip(cj, pj)])
                                 for pe, pv in prow])
                    self.side.append(z3.Implies(_and([e, _not(anynull)]), match))
            for ch in tb.checks:
                from .front import Ctx, FromScope
                sc = FromScope([(t, c.name, ty) for c, (_, ty) in zip(tb.columns, self.tables[t])])
                try:
                    ce = self.front.bool_expr(_strip_tables(ch.expr), Ctx([sc]))
                except Unsupported as u:
                    raise Unsupported("check_constraint", str(u))
                for e, v in rows:
                    errs0 = self.errs
                    self.errs = []
                    val, nul = self.ev(ce, (v,))
                    self.errs = errs0
                    self.side.append(z3.Implies(e, _or([val, nul])))       # CHECK fails only on FALSE

    # .................................................................. relations
    def rel(self, r: R, env=()):
        fd = ir.free_depth(r)
        if fd:
            if fd > len(env):
                raise Unsupported("resolve", "outer reference escapes query")
            key = (r, tuple(id(x) for x in env[len(env) - fd:]))
            self._keep.append(env)
        else:
            key = (r,)
        hit = self.memo.get(key)
        if hit is not None:
            self.stats["memo_hits"] += 1
            res, local = hit
        else:
            self.stats["memo_miss"] += 1
            saved, sg = self.errs, self.guard
            self.errs, self.guard = [], T      # errors are recorded unguarded and guarded at each use
            try:
                self._tick()
                res = getattr(self, "r_" + r.op)(r, env)
                local = self.errs
            finally:
                self.errs, self.guard = saved, sg
            self.memo[key] = (res, local)
        self.errs.extend(_and([self.guard, x]) for x in local)
        return res

    def _with_guard(self, g, f, *a):
        sg = self.guard
        self.guard = _and([sg, g])
        try:
            return f(*a)
        finally:
            self.guard = sg

    def _tick(self):
        if self.deadline is not None and time.perf_counter() > self.deadline:
            raise Unsupported("encoding_timeout", f"k={self.k}")

    def r_scan(self, r, env):
        return Rel(list(self.base[r.args[0]]), r.types)

    def r_values(self, r, env):
        rows = []
        for row in r.args[0]:
            rows.append((T, tuple(self.ev(e, env + ((),)) for e in row)))
        return Rel(rows, r.types)

    def r_filter(self, r, env):
        if joins.is_block_join(r.args[0]):
            leaves, conds = joins.flatten(r.args[0])
            joins.add_cond(conds, r.args[1], 0, r.arity)
            return self._block(r, leaves, conds, env)
        child = self.rel(r.args[0], env)
        out = []
        for e, v in child.rows:
            if z3.is_false(e):
                continue
            pv, pn = self._with_guard(e, self.ev, r.args[1], env + (v,))
            out.append((_and([e, pv, _not(pn)]), v))
        return Rel(out, r.types)

    def r_project(self, r, env):
        child = self.rel(r.args[0], env)
        out = []
        for e, v in child.rows:
            out.append((e, tuple(self._with_guard(e, self.ev, x, env + (v,)) for x in r.args[1])))
        res = Rel(out, r.types)
        if child.pos is not None:
            res.pos, res.det, res.limited = child.pos, child.det, child.limited
        return res

    def _block(self, r, leaves, conds, env):
        """Join block (joins.py): leaves joined in the planned order, each conjunct applied as soon as the leaves it
        reads are present, key-bound leaves joined by if-then-else lookup (one slot per partial row)."""
        rels = [self.rel(leaf, env) for leaf, _ in leaves]
        keys = [joins.leaf_keys(leaf, self.S) for leaf, _ in leaves]
        order, _ = joins.plan(leaves, conds, [len(x.rows) for x in rels], keys)
        width = r.arity
        pending = list(range(len(conds)))
        rows = [(T, [None] * width)]
        have = set()
        for L, lookup in order:
            off = leaves[L][1]
            w = leaves[L][0].arity
            lrows = rels[L].rows
            new = []
            for e, v in rows:
                self._tick()
                if lookup is None:
                    for re_, rv in lrows:
                        both = _and([e, re_])
                        if z3.is_false(both):
                            continue
                        nv = list(v)
                        nv[off:off + w] = rv
                        new.append((both, nv))
                    continue
                ms, cand = [], []
                for re_, rv in lrows:
                    both = _and([e, re_])
                    if z3.is_false(both):
                        continue
                    nv = list(v)
                    nv[off:off + w] = rv
                    m = [re_]
                    for ci in lookup:
                        c, co, cw, _ = conds[ci]
                        m += self._truth(self._with_guard(both, self.ev, c, env + (tuple(nv[co:co + cw]),)))
                    m = _and(m)
                    if not z3.is_false(m):
                        ms.append(m)
                        cand.append(rv)
                if not cand:
                    continue
                sel = []
                for c in range(w):
                    val, nul = cand[-1][c]
                    for m, rv in zip(ms[-2::-1], cand[-2::-1]):
                        val, nul = _ite(m, rv[c][0], val), _ite(m, rv[c][1], nul)
                    sel.append((val, nul))
                nv = list(v)
                nv[off:off + w] = sel
                new.append((_and([e, _or(ms)]), nv))
            rows = new
            have |= set(range(off, off + w))
            if lookup:
                pending = [ci for ci in pending if ci not in lookup]
            ready = [ci for ci in pending if conds[ci][3] <= have]
            pending = [ci for ci in pending if ci not in ready]
            for ci in ready:
                c, co, cw, _ = conds[ci]
                out = []
                for e, v in rows:
                    t = self._truth(self._with_guard(e, self.ev, c, env + (tuple(v[co:co + cw]),)))
                    e2 = _and([e] + t)
                    if not z3.is_false(e2):
                        out.append((e2, v))
                rows = out
        assert not pending
        self.stats["block_lookups"] = self.stats.get("block_lookups", 0) + sum(1 for _, lk in order if lk)
        return Rel([(e, tuple(v)) for e, v in rows], r.types)

    def r_join(self, r, env):
        if joins.is_block_join(r):
            leaves, conds = joins.flatten(r)
            return self._block(r, leaves, conds, env)
        kind, L, Rr, cond = r.args
        lateral = kind.startswith("lat_")
        if lateral:
            kind = kind[4:]
        lrel = self.rel(L, env)
        rrel = None if lateral else self.rel(Rr, env)
        lnull = tuple((self._zero(t), T) for t in L.types)
        rnull = tuple((self._zero(t), T) for t in Rr.types)
        out = []
        match = {}
        for i, (le, lv) in enumerate(lrel.rows):
            self._tick()
            if lateral:
                rrel = self._with_guard(le, self.rel, Rr, env + (lv,))
            for j, (re_, rv) in enumerate(rrel.rows):
                both = _and([le, re_])
                if z3.is_false(both):
                    continue
                if cond is ir.TRUE or cond == ir.TRUE:
                    m = both
                else:
                    cv, cn = self._with_guard(both, self.ev, cond, env + (lv + rv,))
                    m = _and([both, cv, _not(cn)])
                match[i, j] = m
                if kind in ("inner", "cross", "left", "right", "full"):
                    out.append((m, lv + rv))
        if kind in ("semi", "anti"):
            res = []
            for i, (le, lv) in enumerate(lrel.rows):
                any_ = _or([match.get((i, j), F_) for j in range(len(rrel.rows))])
                res.append((_and([le, any_ if kind == "semi" else _not(any_)]), lv))
            return Rel(res, r.types)
        if kind in ("left", "full"):
            for i, (le, lv) in enumerate(lrel.rows):
                any_ = _or([m for (a, _), m in match.items() if a == i])
                out.append((_and([le, _not(any_)]), lv + rnull))
        if kind in ("right", "full"):
            for j, (re_, rv) in enumerate(rrel.rows):
                any_ = _or([match.get((i, j), F_) for i in range(len(lrel.rows))])
                out.append((_and([re_, _not(any_)]), lnull + rv))
        return Rel(out, r.types)

    def r_agg(self, r, env):
        child, keys, aggs = r.args
        c = self.rel(child, env)
        rows = [(e, v) for e, v in c.rows if not z3.is_false(e)]
        kv = [tuple(self._with_guard(e, self.ev, k, env + (v,)) for k in keys) for e, v in rows]
        out = []
        if not keys:
            groups = [(T, [e for e, _ in rows], ())]
        else:
            groups = []
            for i, (e, v) in enumerate(rows):
                self._tick()
                same = [_and([rows[j][0], _row_nseq(kv[j], kv[i])]) for j in range(len(rows))]
                rep = _and([e, _not(_or(same[:i]))])
                if z3.is_false(rep):
                    continue
                ing = [same[j] if j != i else e for j in range(len(rows))]
                groups.append((rep, ing, kv[i]))
        for rep, ing, kval in groups:
            vals = list(kval)
            for a in aggs:
                vals.append(self._with_guard(rep, self._agg, a, rows, ing, env))
            out.append((rep, tuple(vals)))
        return Rel(out, r.types)

    def _agg(self, a: ir.Agg, rows, ing, env):
        if a.filt is not None:
            ing = [_and([g, *self._truth(self.ev(a.filt, env + (v,)))]) for g, (_, v) in zip(ing, rows)]
        if a.func == "count_star":
            return _count(ing), F_
        x = [self.ev(a.args[0], env + (v,)) for _, v in rows]
        valid = [_and([g, _not(n)]) for g, (_, n) in zip(ing, x)]
        if a.distinct and a.func in ("count", "sum", "avg"):
            valid = [_and([valid[j], _not(_or([_and([valid[l], x[l][0] == x[j][0]]) for l in range(j)]))])
                     for j in range(len(x))]
        if a.func == "count":
            return _count(valid), F_
        none = _not(_or(valid))
        zero = self._zero(a.ty if a.func != "avg" else FLOAT)
        if a.func in ("sum", "avg"):
            s = _sum([_ite(g, xv, zero if a.func == "sum" else z3.RealVal(0)) for g, (xv, _) in zip(valid, x)
                      if not z3.is_false(g)], zero)
            if a.func == "sum":
                return s, none
            cnt = _count(valid)
            return _ite(none, z3.RealVal(0), _to_real(s) / z3.ToReal(cnt)), none
        if a.func in ("min", "max"):
            acc_v, acc_n = self._zero(a.ty), T
            for g, (xv, _) in zip(valid, x):
                if z3.is_false(g):
                    continue
                better = xv < acc_v if a.func == "min" else xv > acc_v
                take = _and([g, _or([acc_n, better])])
                acc_v = _ite(take, xv, acc_v)
                acc_n = _and([acc_n, _not(g)])
            return acc_v, acc_n
        if a.func == "any_value":
            acc_v, acc_n = self._zero(a.ty), T
            for g, (xv, xn) in reversed(list(zip(ing, x))):
                acc_v = _ite(g, xv, acc_v)
                acc_n = _ite(g, xn, acc_n)
            return acc_v, acc_n
        raise Unsupported("agg_func", a.func)

    def r_window(self, r, env):
        child, wins = r.args
        c = self.rel(child, env)
        rows = c.rows
        n = len(rows)
        out_extra = [[] for _ in range(n)]
        for w in wins:
            pk = [tuple(self._with_guard(e, self.ev, p, env + (v,)) for p in w.partition) for e, v in rows]
            ok = [tuple(self._with_guard(e, self.ev, o[0], env + (v,)) for o in w.order) for e, v in rows]
            dirs = [(o[1], o[2]) for o in w.order]
            for i in range(n):
                self._tick()
                peers = [_and([rows[j][0], _row_nseq(pk[j], pk[i])]) for j in range(n)]
                before = [_before(ok[j], ok[i], dirs) for j in range(n)]
                tie = [_row_nseq(ok[j], ok[i]) for j in range(n)]
                g_i = rows[i][0]
                if w.func in ("row_number", "rank", "dense_rank") or w.frame == "rows_to_current":
                    if w.func == "row_number" or w.frame == "rows_to_current":
                        # ties inside a partition make the result engine-dependent
                        self.errs.append(_and([self.guard, g_i] + [_or([_and([peers[j], tie[j]]) for j in range(n)
                                                                         if j != i])]))
                if w.func == "row_number":
                    val = 1 + _count([_and([peers[j], _or([before[j], _and([tie[j], z3.BoolVal(j < i)])])])
                                      for j in range(n) if j != i])
                    out_extra[i].append((val, F_))
                    continue
                if w.func == "rank":
                    out_extra[i].append((1 + _count([_and([peers[j], before[j]]) for j in range(n)]), F_))
                    continue
                if w.func == "dense_rank":
                    firsts = []
                    for j in range(n):
                        dup = _or([_and([peers[l], _row_nseq(ok[l], ok[j])]) for l in range(j)])
                        firsts.append(_and([peers[j], before[j], _not(dup)]))
                    out_extra[i].append((1 + _count(firsts), F_))
                    continue
                if w.frame == "whole" or not w.order:
                    member = peers
                elif w.frame == "rows_to_current":
                    member = [_and([peers[j], _or([before[j], _and([tie[j], z3.BoolVal(j <= i)])])]) for j in range(n)]
                else:
                    member = [_and([peers[j], _or([before[j], tie[j]])]) for j in range(n)]
                dist = w.func.endswith("_distinct")
                fake = ir.Agg(w.func[:-9] if dist else w.func, dist, w.args, w.ty)
                out_extra[i].append(self._with_guard(g_i, self._agg, fake, rows, member, env))
        return Rel([(e, v + tuple(x)) for (e, v), x in zip(rows, out_extra)], r.types)

    def r_distinct_on(self, r, env):
        """Row i is kept iff no other existing row with the same DISTINCT ON key sorts before it (ties: the earlier
        slot). Two tied rows of one key that differ anywhere make the kept row engine-dependent: runtime error."""
        child, on, okeys = r.args
        c = self.rel(child, env)
        rows = c.rows
        n = len(rows)
        kv = [tuple(v[i] for i in on) for _, v in rows]
        ov = [tuple(self._with_guard(e, self.ev, k[0], env + (v,)) for k in okeys) for e, v in rows]
        dirs = [(k[1], k[2]) for k in okeys]
        out, amb = [], []
        for i in range(n):
            self._tick()
            beat = []
            for j in range(n):
                if j == i:
                    continue
                same = _and([rows[j][0], _row_nseq(kv[j], kv[i])])
                tie = _row_nseq(ov[j], ov[i]) if okeys else T
                beat.append(_and([same, _or([_before(ov[j], ov[i], dirs), _and([tie, z3.BoolVal(j < i)])])]))
                if j < i:
                    amb.append(_and([rows[i][0], same, tie, _not(_row_nseq(rows[j][1], rows[i][1]))]))
            out.append((_and([rows[i][0], _not(_or(beat))]), rows[i][1]))
        self.errs.append(_or(amb))
        return Rel(out, r.types)

    def r_distinct(self, r, env):
        c = self.rel(r.args[0], env)
        return Rel(_dedup(c.rows), r.types)

    def r_setop(self, r, env):
        kind, all_, L, Rr = r.args
        a, b = self.rel(L, env), self.rel(Rr, env)
        if kind == "union":
            rows = list(a.rows) + list(b.rows)
            return Rel(rows if all_ else _dedup(rows), r.types)
        arows = a.rows if all_ else _dedup(a.rows)
        out = []
        for i, (e, v) in enumerate(arows):
            self._tick()
            in_b = [_and([be, _row_nseq(bv, v)]) for be, bv in b.rows]
            if all_:
                rank = _count([_and([arows[j][0], _row_nseq(arows[j][1], v)]) for j in range(i)])
                cnt = _count(in_b)
                keep = rank < cnt if kind == "intersect" else rank >= cnt
            else:
                keep = _or(in_b) if kind == "intersect" else _not(_or(in_b))
            out.append((_and([e, keep]), v))
        return Rel(out, r.types)

    def r_sort(self, r, env):
        child, keys, lim, off = r.args
        c = self.rel(child, env)
        rows = c.rows
        n = len(rows)
        kv = [tuple(self._with_guard(e, self.ev, k[0], env + (v,)) for k in keys) for e, v in rows]
        dirs = [(k[1], k[2]) for k in keys]
        # A tie is legal SQL, including a LIMIT/OFFSET boundary.  Represent
        # every tie completion rather than discarding those input databases.
        # The priorities form a permutation of all slots; restricting to live
        # slots still admits every linear extension of the ORDER BY keys.
        limited = lim is not None or off is not None
        priority = [z3.FreshInt(f"{self.pfx}_tie") for _ in rows] if limited else []
        if priority:
            self.side.extend(z3.And(p >= 0, p < n) for p in priority)
            if n > 1:
                self.side.append(z3.Distinct(*priority))
        pos, ties = [], []
        for i in range(n):
            self._tick()
            terms = []
            for j in range(n):
                if j == i:
                    continue
                bf = _before(kv[j], kv[i], dirs)
                tie = _row_nseq(kv[j], kv[i]) if keys else T
                tie_before = priority[j] < priority[i] if limited else z3.BoolVal(j < i)
                terms.append(_and([rows[j][0], _or([bf, _and([tie, tie_before])])]))
                if j < i:
                    ties.append(_and([rows[i][0], rows[j][0], tie]))
            pos.append(_count(terms))
        det = _not(_or(ties))
        if limited:
            o = off or 0
            out, keep = [], []
            for (e, v), p in zip(rows, pos):
                cond = [e, p >= o]
                if lim is not None:
                    cond.append(p < o + lim)
                keep.append(_and(cond))
                out.append((keep[-1], v))
            # Ties change the admissible selected values, not runtime validity.
            pos = [p - o for p in pos]
            return Rel(out, r.types, pos, det, True)
        return Rel(list(rows), r.types, pos, det, False)

    # .................................................................. scalar expressions
    def _zero(self, ty):
        if ty == BOOL:
            return F_
        if ty == FLOAT or is_dec(ty):
            return z3.RealVal(0)
        return z3.IntVal(0)

    def _truth(self, vn):
        v, n = vn
        return [v, _not(n)]

    def ev(self, e: E, env):
        m = getattr(self, "e_" + e.op)
        return m(e, env)

    def e_col(self, e, env):
        return env[-1][e.args[0]]

    def e_outer(self, e, env):
        d, i = e.args
        return env[-1 - d][i]

    def e_lit(self, e, env):
        v, ty = e.args[0], e.ty
        if v is None:
            return self._zero(ty if ty != NULL else INT), T
        if ty == BOOL:
            return z3.BoolVal(bool(v)), F_
        if ty == INT:
            return z3.IntVal(int(v)), F_
        if is_dec(ty) or ty == FLOAT:
            return z3.RealVal(Fraction(str(v)) if not isinstance(v, float) else Fraction(v)), F_
        if ty == STR:
            c = self.dom.code(v)
            if c is None:
                raise Unsupported("domain", f"string literal outside pool: {v[:40]}")
            return z3.IntVal(c), F_
        if ty == DATE:
            return z3.IntVal(day_of(v)), F_
        if ty == TS:
            return z3.IntVal(int((v - dt.datetime(1970, 1, 1)).total_seconds())), F_
        raise Unsupported("literal_type", ty)

    def e_case(self, e, env):
        whens, el = e.args
        v, n = self.ev(el, env)
        for c, r in reversed(whens):
            cv, cn = self.ev(c, env)
            t = _and([cv, _not(cn)])
            rv, rn = self.ev(r, env)
            v, n = _ite(t, rv, v), _ite(t, rn, n)
        return v, n

    def e_win(self, e, env):
        raise Unsupported("window_position")

    def e_sub(self, e, env):
        kind, rel, lhs, op, quant = e.args
        sub = self.rel(rel, env)
        rows = [(x, v) for x, v in sub.rows if not z3.is_false(x)]
        if kind == "exists":
            return _or([x for x, _ in rows]), F_
        if kind == "scalar":
            cnt = _count([x for x, _ in rows])
            if len(rows) > 1:
                self.errs.append(_and([self.guard, cnt > 1]))
            v, n = self._zero(e.ty), T
            for x, val in reversed(rows):
                v, n = _ite(x, val[0][0], v), _ite(x, val[0][1], n)
            return v, n
        lv = [self.ev(a, env) for a in lhs]
        xs, tts, ffs = [], [], []
        for x, val in rows:
            cs = [_cmp3(op, a, b) for a, b in zip(lv, val)]
            # row comparison: TRUE iff every component TRUE, FALSE iff some component FALSE
            xs.append(x)
            tts.append(_and([c[0] for c in cs]))
            ffs.append(_or([c[1] for c in cs]))
        if quant == "any":
            Tr = _or([_and([x, t]) for x, t in zip(xs, tts)])
            Fa = _and([_or([_not(x), f]) for x, f in zip(xs, ffs)])
        else:
            Tr = _and([_or([_not(x), t]) for x, t in zip(xs, tts)])
            Fa = _or([_and([x, f]) for x, f in zip(xs, ffs)])
        return Tr, _and([_not(Tr), _not(Fa)])

    def e_fn(self, e, env):
        name = e.args[0]
        args = e.args[1:]
        m = getattr(self, "f_" + name, None)
        if m is None:
            raise Unsupported("function", name)
        return m(e, args, env)

    # logic
    def f_and(self, e, a, env):
        x, y = self.ev(a[0], env), self.ev(a[1], env)
        t = _and([x[0], _not(x[1]), y[0], _not(y[1])])
        f = _or([_and([_not(x[0]), _not(x[1])]), _and([_not(y[0]), _not(y[1])])])
        return t, _and([_not(t), _not(f)])

    def f_or(self, e, a, env):
        x, y = self.ev(a[0], env), self.ev(a[1], env)
        t = _or([_and([x[0], _not(x[1])]), _and([y[0], _not(y[1])])])
        f = _and([_not(x[0]), _not(x[1]), _not(y[0]), _not(y[1])])
        return t, _and([_not(t), _not(f)])

    def f_not(self, e, a, env):
        v, n = self.ev(a[0], env)
        return _not(v), n

    def f_is_null(self, e, a, env):
        return self.ev(a[0], env)[1], F_

    def f_is_true(self, e, a, env):
        v, n = self.ev(a[0], env)
        return _and([v, _not(n)]), F_

    def f_is_false(self, e, a, env):
        v, n = self.ev(a[0], env)
        return _and([_not(v), _not(n)]), F_

    def f_nseq(self, e, a, env):
        return _nseq(self.ev(a[0], env), self.ev(a[1], env)), F_

    def _cmp(self, op, a, env):
        x, y = self.ev(a[0], env), self.ev(a[1], env)
        return _cmpv(op, x[0], y[0]), _or([x[1], y[1]])

    def f_eq(self, e, a, env):
        return self._cmp("eq", a, env)

    def f_neq(self, e, a, env):
        return self._cmp("neq", a, env)

    def f_lt(self, e, a, env):
        return self._cmp("lt", a, env)

    def f_le(self, e, a, env):
        return self._cmp("le", a, env)

    def f_gt(self, e, a, env):
        return self._cmp("gt", a, env)

    def f_ge(self, e, a, env):
        return self._cmp("ge", a, env)

    def f_like(self, e, a, env):
        x, n = self.ev(a[0], env)
        pat, esc, ci = a[1].args[0], a[2].args[0], a[3].args[0]
        hits = [i for i, s in enumerate(self.dom.strings) if like(s, pat, esc, ci)]
        return self._member(x, hits), n

    def f_regex(self, e, a, env):
        import re
        x, n = self.ev(a[0], env)
        rx = re.compile(a[1].args[0])
        hits = [i for i, s in enumerate(self.dom.strings) if rx.search(s)]
        return self._member(x, hits), n

    def _member(self, x, hits):
        P = len(self.dom.strings)
        if len(hits) > P // 2:
            miss = sorted(set(range(P)) - set(hits))
            return _and([x != i for i in miss])
        return _or([x == i for i in hits])

    # arithmetic
    def f_add(self, e, a, env):
        x, y = self.ev(a[0], env), self.ev(a[1], env)
        return _num(x[0], e.ty) + _num(y[0], e.ty), _or([x[1], y[1]])

    def f_sub(self, e, a, env):
        x, y = self.ev(a[0], env), self.ev(a[1], env)
        return _num(x[0], e.ty) - _num(y[0], e.ty), _or([x[1], y[1]])

    def f_mul(self, e, a, env):
        x, y = self.ev(a[0], env), self.ev(a[1], env)
        return _num(x[0], e.ty) * _num(y[0], e.ty), _or([x[1], y[1]])

    def f_neg(self, e, a, env):
        x = self.ev(a[0], env)
        return -x[0], x[1]

    def f_div(self, e, a, env):
        x, y = self.ev(a[0], env), self.ev(a[1], env)
        nul = _or([x[1], y[1]])
        zero = y[0] == 0
        if self.dialect in ("sqlite", "mysql"):
            nul = _or([nul, zero])
        else:                       # PostgreSQL raises, DuckDB returns +-inf: keep witnesses away from it
            self.errs.append(_and([self.guard, _not(nul), zero]))
        return _to_real(x[0]) / _to_real(y[0]), nul

    def f_idiv(self, e, a, env):
        x, y = self.ev(a[0], env), self.ev(a[1], env)
        nul = _or([x[1], y[1]])
        zero = y[0] == 0
        if self.dialect == "postgres":
            self.errs.append(_and([self.guard, _not(nul), zero]))
        else:
            nul = _or([nul, zero])
        ax, ay = _abs(x[0]), _abs(y[0])
        q = ax / ay
        return _ite((x[0] >= 0) == (y[0] > 0), q, -q), nul

    def f_mod(self, e, a, env):
        x, y = self.ev(a[0], env), self.ev(a[1], env)
        if e.ty != INT:
            raise Unsupported("mod_type", e.ty)
        nul = _or([x[1], y[1]])
        zero = y[0] == 0
        if self.dialect == "postgres":
            self.errs.append(_and([self.guard, _not(nul), zero]))
        else:
            nul = _or([nul, zero])
        ax, ay = _abs(x[0]), _abs(y[0])
        r = ax % ay
        return _ite(x[0] >= 0, r, -r), nul

    def f_abs(self, e, a, env):
        x = self.ev(a[0], env)
        return _abs(x[0]), x[1]

    def f_round(self, e, a, env):
        x = self.ev(a[0], env)
        nd = a[1].args[0]
        return _round(_to_real(x[0]), nd, e.ty), x[1]

    def f_floor(self, e, a, env):
        x = self.ev(a[0], env)
        if a[0].ty == INT:
            return x
        v = z3.ToInt(_to_real(x[0]))
        return (v if e.ty == INT else z3.ToReal(v)), x[1]

    def f_ceil(self, e, a, env):
        x = self.ev(a[0], env)
        if a[0].ty == INT:
            return x
        v = -z3.ToInt(-_to_real(x[0]))
        return (v if e.ty == INT else z3.ToReal(v)), x[1]

    def f_cast_num(self, e, a, env):
        x = self.ev(a[0], env)
        src, dst = a[0].ty, e.ty
        if src == NULL:
            return self._zero(dst), T
        if dst == FLOAT:
            return _to_real(x[0]), x[1]
        if dst == INT:
            if src == INT:
                return x
            if self.dialect == "sqlite":
                v = _to_real(x[0])
                return _ite(v >= 0, z3.ToInt(v), -z3.ToInt(-v)), x[1]
            return _round(_to_real(x[0]), 0, INT), x[1]
        if is_dec(dst):
            if src == INT:
                return z3.ToReal(x[0]), x[1]
            s = ir.dec_scale(dst)
            if is_dec(src) and ir.dec_scale(src) <= s:
                return x
            return _round(_to_real(x[0]), s, dst), x[1]
        raise Unsupported("cast", f"{src}->{dst}")

    def f_coalesce(self, e, a, env):
        vals = [self.ev(x, env) for x in a]
        v, n = vals[-1]
        for xv, xn in reversed(vals[:-1]):
            v, n = _ite(xn, v, xv), _and([xn, n])
        return v, n

    # strings over the pool
    def f_strmap(self, e, a, env):
        x, n = self.ev(a[0], env)
        name, *ps = a[1].args[0]
        f = STR_MAPS[name]
        P = self.dom.strings
        out = z3.IntVal(0)
        nul = n
        bad, nulls = [], []
        table = []
        for i, s in enumerate(P):
            try:
                r = f(s, *ps)
            except Exception:
                r = None
            if r is None:
                nulls.append(i)
                table.append(0)
                continue
            c = self.dom.code(r)
            if c is None:
                bad.append(i)
                table.append(0)
            else:
                table.append(c)
        out = _lookup(x, table)
        if bad:
            self.errs.append(_and([self.guard, _not(n), _or([x == i for i in bad])]))
        if nulls:
            nul = _or([n, _or([x == i for i in nulls])])
        return out, nul

    def f_strnum(self, e, a, env):
        x, n = self.ev(a[0], env)
        name, *ps = a[1].args[0]
        f = STR_NUMS[name]
        return _lookup(x, [f(s, *ps) for s in self.dom.strings]), n

    # dates (day numbers within [dlo, dhi])
    def f_date_part(self, e, a, env):
        x, n = self.ev(a[0], env)
        unit = a[1].args[0]
        d = self.dom
        if unit == "year":
            v = d.ylo + _count([x >= day_of(dt.date(y, 1, 1)) for y in range(d.ylo + 1, d.yhi + 2)])
            return v, n
        starts = d.month_starts
        if unit == "month":
            v = z3.IntVal(1)
            for s in starts:
                v = _ite(x >= s, z3.IntVal(date_of(s).month), v)
            return v, n
        if unit == "day":
            v = x - starts[0] + 1
            for s in starts:
                v = _ite(x >= s, x - s + 1, v)
            return v, n
        raise Unsupported("extract_unit", unit)

    def f_date_trunc(self, e, a, env):
        x, n = self.ev(a[0], env)
        unit = a[1].args[0]
        d = self.dom
        if unit == "year":
            v = z3.IntVal(day_of(dt.date(d.ylo, 1, 1)))
            for y in range(d.ylo, d.yhi + 2):
                s = day_of(dt.date(y, 1, 1))
                v = _ite(x >= s, z3.IntVal(s), v)
            return v, n
        if unit == "month":
            v = z3.IntVal(d.month_starts[0])
            for s in d.month_starts:
                v = _ite(x >= s, z3.IntVal(s), v)
            return v, n
        raise Unsupported("date_trunc_unit", unit)

    def f_date_add_days(self, e, a, env):
        x, y = self.ev(a[0], env), self.ev(a[1], env)
        return x[0] + y[0], _or([x[1], y[1]])

    def f_date_diff_days(self, e, a, env):
        x, y = self.ev(a[0], env), self.ev(a[1], env)
        return x[0] - y[0], _or([x[1], y[1]])

    def f_date_add_months(self, e, a, env):
        x, n = self.ev(a[0], env)
        m = a[1].args[0]
        v = x
        for s in self.dom.month_starts:
            d0 = date_of(s)
            tgt = add_months(d0, m)
            off = day_of(tgt) - s
            nxt = add_months(tgt, 1)
            last = day_of(nxt) - 1
            v = _ite(x >= s, _min(x + off, z3.IntVal(last)), v)
        return v, n

    def f_ts_date(self, e, a, env):
        x, n = self.ev(a[0], env)
        return x / 86400, n                  # z3 Int division floors for a positive divisor

    def f_date_ts(self, e, a, env):
        x, n = self.ev(a[0], env)
        return x * 86400, n


# ---------------------------------------------------------------------- helpers
def _strip_tables(e):
    e = e.copy()
    from sqlglot import exp
    for c in e.find_all(exp.Column):
        c.set("table", None)
    return e


def _to_real(x):
    if isinstance(x, z3.ArithRef) and x.is_int():
        return z3.ToReal(x)
    return x


def _num(x, ty):
    if ty == FLOAT or is_dec(ty):
        return _to_real(x)
    return x


def _abs(x):
    return _ite(x >= 0, x, -x)


def _min(a, b):
    return _ite(a <= b, a, b)


def _round(x, nd, ty):
    """Round half away from zero to nd decimals (PostgreSQL numeric / DuckDB DECIMAL rule)."""
    s = 10 ** nd if nd >= 0 else Fraction(1, 10 ** (-nd))
    y = x * s
    r = _ite(y >= 0, z3.ToInt(y + Fraction(1, 2)), -z3.ToInt(-y + Fraction(1, 2)))
    if ty == INT:
        return r if nd == 0 else z3.ToInt(z3.ToReal(r) / s)
    return z3.ToReal(r) / s


def _lookup(x, table):
    if not table:
        return z3.IntVal(0)
    v = z3.IntVal(table[-1])
    for i in range(len(table) - 2, -1, -1):
        if table[i] == table[-1] and False:
            continue
        v = _ite(x == i, z3.IntVal(table[i]), v)
    return v


def _cmpv(op, a, b):
    if op == "eq":
        return a == b
    if op == "neq":
        return a != b
    if op == "lt":
        return a < b
    if op == "le":
        return a <= b
    if op == "gt":
        return a > b
    if op == "ge":
        return a >= b
    raise ValueError(op)


def _cmp3(op, a, b):
    """(TRUE, FALSE) conditions of a three-valued comparison."""
    nn = _and([_not(a[1]), _not(b[1])])
    c = _cmpv(op, a[0], b[0])
    return _and([nn, c]), _and([nn, _not(c)])


def _nseq(a, b):
    return _or([_and([a[1], b[1]]), _and([_not(a[1]), _not(b[1]), a[0] == b[0]])])


def _row_nseq(u, v):
    return _and([_nseq(a, b) for a, b in zip(u, v)])


def _before(u, v, dirs):
    """u sorts strictly before v under ORDER BY directions dirs = [(desc, nulls_first)]."""
    out = F_
    for (a, b, (desc, nf)) in reversed(list(zip(u, v, dirs))):
        lt = (a[0] > b[0]) if desc else (a[0] < b[0])
        strict = _or([
            _and([a[1], _not(b[1])]) if nf else _and([_not(a[1]), b[1]]),
            _and([_not(a[1]), _not(b[1]), lt]),
        ])
        out = _or([strict, _and([_nseq(a, b), out])])
    return out


def _dedup(rows):
    out = []
    for i, (e, v) in enumerate(rows):
        dup = _or([_and([rows[j][0], _row_nseq(rows[j][1], v)]) for j in range(i)])
        out.append((_and([e, _not(dup)]), v))
    return out
