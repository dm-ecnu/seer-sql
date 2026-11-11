"""Aggregate-aware witnesses: multi-row, multi-group instances whose numeric values are chosen by Z3 arithmetic over
SUM / AVG / COUNT / MIN / MAX.

Targets (diff.py) name one atom A of query q that compares against an aggregate:
  AGG(A, "ctx")    A = <expr> op (scalar subquery with an aggregate), correlated or not. Some outer row must get a
                   different truth value from its own aggregate context than from another context: the decorrelated
                   one (correlation predicates dropped, i.e. the whole table) or the context of another outer row
                   (another correlation key). This is what separates a correct correlated subquery from a wrongly
                   decorrelated / wrongly correlated rewrite (TPC-H Q17, Q20, Q2 style).
  AGG(A, "mixed")  A is a HAVING atom over aggregates: one group passes and another fails (a threshold between them).
  AGG(A, "edge")   A is a HAVING atom: some group sits exactly on the threshold (< vs <=, c vs c+-1).

Construction:
  1. skeleton: a witness unit of q (generate.Witness, all asserted formulas TRUE), a second unit whose FKs may not
     point into the first (a different group / correlation key), and two extra copies of the aggregated occurrence's
     row in every unit (same FKs, fresh keys: same group). Only rows, keys and non-numeric values are fixed here.
  2. membership by execution: the scope of A is rewritten (A replaced by TRUE, projection replaced by the rowids of its
     occurrences and LIST(rowid) of the aggregated occurrence, per outer row for a subquery -- correlated and
     decorrelated -- or per group for HAVING) and run in DuckDB on the skeleton. The query's own scoping decides which
     rows feed which aggregate; nothing about correlation is re-implemented.
  3. numeric columns referenced by A and by no other atom / join / group key of q become Z3 integers at the column scale
     (one per row), with bounds from the column pool; A is translated to Z3 linear arithmetic per outer row / group
     (AVG = SUM / COUNT with the count fixed by the skeleton); the goal above is asserted, with random value
     preferences for diversity.
  4. the model's values are written back and the instance is re-checked by Schema.violations.
Unsupported shapes (aggregate over several occurrences, numeric column also used elsewhere, DISTINCT aggregates,
non-linear arithmetic) return "fail"; they are counted, never guessed.
"""
from __future__ import annotations

import copy
import decimal
import random

import z3
from sqlglot import exp

from .backends import DuckDBBackend
from .query import CMP, QInfo, atoms_of
from .schema import DEC, FLOAT, INT

NUM = (INT, DEC, FLOAT)
COPIES = 2                  # extra rows of the aggregated occurrence per unit
Z3_TIMEOUT_MS = 3000


def agg_atoms(qi: QInfo):
    """[(atom key, mode)] of the AGG targets of a query."""
    out = []
    if not qi.ok:
        return out
    for f in qi.formulas:
        for a in atoms_of(f.node):
            if not isinstance(a, CMP):
                continue
            key = qi.norm_sql(a)
            sq = _agg_subquery(a)
            if sq is not None and f.kind in ("where", "on"):
                out.append((key, "ctx"))
            elif f.kind == "having" and a.find(exp.AggFunc) is not None and a.find(exp.Select) is None:
                out += [(key, "mixed"), (key, "edge")]
    return list(dict.fromkeys(out))


def _agg_subquery(a):
    for sub in a.find_all(exp.Subquery):
        sel = sub.this
        if isinstance(sel, exp.Select) and len(sel.expressions) == 1 and sel.expressions[0].find(exp.AggFunc) \
                is not None and not sel.args.get("group"):
            return sub
    return None


def _walk_map(a, b):
    """id(node of tree a) -> node of copy b (same walk order)."""
    return {id(x): y for x, y in zip(a.walk(), b.walk())}


class AggWitness:
    def __init__(self, schema, pools, wit, mut, seed=0):
        self.S, self.pools, self.wit, self.mut = schema, pools, wit, mut
        self.rng = random.Random(f"agg-{seed}")
        self.stats = {"sat": 0, "unsat": 0, "fail": 0}

    # ------------------------------------------------------------------ entry
    def realize(self, qi: QInfo, key, mode):
        try:
            st, db = self._realize(qi, key, mode)
        except _Unsupported:
            st, db = "fail", None
        self.stats[st] += 1
        return st, db

    def _realize(self, qi, key, mode):
        S = self.S
        found = [(f, a) for f in qi.formulas for a in atoms_of(f.node) if qi.norm_sql(a) == key]
        if not found:
            raise _Unsupported
        f, A = found[0]
        sc = qi.scopes[f.sid]
        if mode == "ctx":
            sq = _agg_subquery(A)
            if sq is None:
                raise _Unsupported
            inner = qi.scopes.get(id(sq.this))
            if inner is None or len(inner.occs) != 1:
                raise _Unsupported
            agg_oid = inner.occs[0]
        else:
            oids = {qi.resolve(c)[0] for g in A.find_all(exp.AggFunc) for c in g.find_all(exp.Column)
                    if qi.resolve(c) is not None}
            if len(oids) != 1:
                raise _Unsupported
            agg_oid = oids.pop()
        agg_t = qi.occ_by_id(agg_oid).table
        nv = self._numeric_vars(qi, A)
        if not nv:
            raise _Unsupported
        db = self._skeleton(qi, agg_oid)
        if db is None:
            return "fail", None
        rows = self._membership(qi, sc, A, agg_oid, mode, db)
        if rows is None:
            raise _Unsupported
        return self._solve(qi, sc, A, agg_oid, agg_t, mode, nv, db, rows)

    # ------------------------------------------------------------------ 1. skeleton
    def _skeleton(self, qi, agg_oid):
        S = self.S
        db = {t: [] for t in S.order}
        units = []
        for disjoint in (False, True):
            st, nd, info = None, None, None
            for m in ("strict", "loose"):
                st, nd, info = self.wit.solve(qi, db, mode=m, disjoint=disjoint)
                if st == "sat":
                    break
            if st != "sat":
                if not units:
                    return None
                break
            db = nd
            units.append(info)
        for info in units:
            if agg_oid not in info["oids"]:
                continue
            t, row = info["oids"][agg_oid]
            pk = S.tables[t].pk or S.tables[t].colnames
            r = next((x for x in db[t] if all(x[c] == row[c] for c in pk)), None)
            if r is None:
                continue
            for _ in range(COPIES):
                self.mut.clone(db, t, r, deep=False)
        S.check(db)
        return db

    # ------------------------------------------------------------------ 2. membership
    def _membership(self, qi, sc, A, agg_oid, mode, db):
        root = qi.tree
        if not isinstance(root, exp.Select):
            return None
        rc = root.copy()
        mp = _walk_map(root, rc)
        Sc = mp.get(id(sc.node))
        if Sc is None:
            return None
        aliases = [(o, qi.occ_by_id(o).alias) for o in sc.occs]
        agg_alias = qi.occ_by_id(agg_oid).alias
        cols = []
        if mode == "ctx":
            sq = _agg_subquery(A)
            sub = mp[id(sq.this)].copy()
            corr = []
            w = sq.this.args.get("where")
            if w is not None:
                inner_occs = set(qi.scopes[id(sq.this)].occs)
                wc = mp[id(w.this)]
                m2 = _walk_map(wc, sub.args["where"].this)
                for a in atoms_of(w.this):
                    rs = [qi.resolve(c) for c in a.find_all(exp.Column)]
                    if any(r is None for r in rs):
                        return None
                    if any(r[0] not in inner_occs for r in rs):
                        corr.append(m2[id(mp[id(a)])])
            lst = exp.func("list", exp.column("rowid", table=agg_alias))
            sub.set("expressions", [lst])
            for k in ("group", "having", "order", "limit"):
                sub.set(k, None)
            dec = sub.copy()
            if corr:
                m3 = _walk_map(sub, dec)
                for c in corr:
                    m3[id(c)].replace(exp.true())
            cols = [exp.column("rowid", table=al) for _, al in aliases] + [exp.Subquery(this=sub),
                                                                             exp.Subquery(this=dec)]
            mp[id(A)].replace(exp.true())
            for k in ("group", "having", "order", "limit", "distinct", "qualify"):
                Sc.set(k, None)
        else:
            mp[id(A)].replace(exp.true())
            h = Sc.args.get("having")
            if h is not None and isinstance(h.this, exp.Boolean):
                Sc.set("having", None)
            # other HAVING atoms stay (they filter groups); A itself is TRUE
            cols = [exp.func("list", exp.column("rowid", table=agg_alias))]
            Sc.set("order", None)
            Sc.set("limit", None)
            Sc.set("distinct", None)
        Sc.set("expressions", cols)
        if Sc is not rc:
            w = rc.args.get("with")
            if w is not None:
                Sc.set("with", w.copy())
        sql = Sc.sql(dialect="duckdb")
        inst = DuckDBBackend(self.S).open(db)
        try:
            res, _ = inst.run(sql)
        finally:
            inst.close()
        if not isinstance(res, list):
            return None
        out = []
        n = len(aliases)
        for r in res:
            if mode == "ctx":
                out.append({"rowids": dict(zip([o for o, _ in aliases], r[:n])), "corr": list(r[n] or []),
                            "decor": list(r[n + 1] or [])})
            else:
                out.append({"members": list(r[0] or [])})
        return out

    # ------------------------------------------------------------------ 3. numeric variables
    def _numeric_vars(self, qi, A):
        """(table, col) of numeric non-key columns referenced in A and nowhere else in the query's formulas,
        join conditions, group keys or subquery WHERE clauses."""
        S = self.S
        inA = {}
        for c in A.find_all(exp.Column):
            r = qi.column_table(c)
            if r is not None and not S.is_key(*r) and S.column(*r).type in NUM:
                inA[r] = True
        if not inA:
            return []
        own = set(id(x) for x in A.walk())
        sq = _agg_subquery(A)
        sub_where = set()
        if sq is not None and sq.this.args.get("where") is not None:
            sub_where = set(id(x) for x in sq.this.args["where"].walk())
        used = set()
        for f in qi.formulas:
            for a in atoms_of(f.node):
                for c in a.find_all(exp.Column):
                    if id(c) in own and id(c) not in sub_where:
                        continue
                    r = qi.column_table(c)
                    if r is not None:
                        used.add(r)
        for s_ in qi.scopes.values():
            g = s_.node.args.get("group")
            if g is not None:
                for c in g.find_all(exp.Column):
                    r = qi.column_table(c)
                    if r is not None:
                        used.add(r)
        return [r for r in inA if r not in used]

    def _scale(self, t, c):
        col = self.S.column(t, c)
        if col.type == INT:
            return 0
        if col.type == DEC:
            return (col.param or (18, 2))[1]
        return 2

    # ------------------------------------------------------------------ 4. solve
    def _solve(self, qi, sc, A, agg_oid, agg_t, mode, nv, db, rows):
        S = self.S
        s = z3.Solver()
        s.set("timeout", Z3_TIMEOUT_MS)
        s.set("random_seed", self.rng.randrange(1 << 30))
        X = {}
        prefs = []
        for t, c in nv:
            sc_ = self._scale(t, c)
            pool = [v for v in self.pools.col[(t, c)] if v is not None]
            lo = min(pool) if pool else 0
            hi = max(pool) if pool else 100
            lo = min(lo, 0) if lo < 0 else 0
            hi = max(hi, 100)
            f = 10 ** sc_
            for i, _ in enumerate(db[t]):
                k = z3.Int(f"n.{t}.{c}.{i}")
                s.add(k >= int(lo * f), k <= int(hi * f))
                X[(t, c, i)] = (k, f)
                prefs.append((k, int(lo * f), int(min(hi, lo + 60) * f)))

        def val(t, c, i):
            if (t, c, i) in X:
                k, f = X[(t, c, i)]
                return z3.ToReal(k) / f
            v = db[t][i][c]
            if v is None:
                raise _Null
            if isinstance(v, bool) or not isinstance(v, (int, float, decimal.Decimal)):
                raise _Unsupported
            return z3.RealVal(str(v))

        def tr(node, outer, members):
            """Z3 term of a numeric expression; outer: {oid: rowid}; members: rowids of the aggregated occurrence."""
            if isinstance(node, exp.Paren):
                return tr(node.this, outer, members)
            if isinstance(node, exp.Literal) and not node.is_string:
                return z3.RealVal(node.this)
            if isinstance(node, exp.Neg):
                return -tr(node.this, outer, members)
            if isinstance(node, exp.Cast):
                return tr(node.this, outer, members)
            if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
                a, b = tr(node.this, outer, members), tr(node.expression, outer, members)
                if isinstance(node, exp.Add):
                    return a + b
                if isinstance(node, exp.Sub):
                    return a - b
                if isinstance(node, exp.Mul):
                    if not (z3.is_rational_value(z3.simplify(a)) or z3.is_rational_value(z3.simplify(b))):
                        raise _Unsupported
                    return a * b
                if not z3.is_rational_value(z3.simplify(b)):
                    raise _Unsupported
                return a / b
            if isinstance(node, exp.Subquery):
                sel = node.this
                return tr(sel.expressions[0].unalias(), outer, members)
            if isinstance(node, exp.AggFunc):
                arg = node.this
                if isinstance(arg, exp.Distinct):
                    raise _Unsupported
                if not members:
                    raise _Null
                if isinstance(node, exp.Count):
                    return z3.RealVal(len(members))
                terms = [tr(arg, {agg_oid: m}, None) for m in members]
                if isinstance(node, exp.Sum):
                    return z3.Sum(terms)
                if isinstance(node, exp.Avg):
                    return z3.Sum(terms) / len(members)
                if isinstance(node, (exp.Min, exp.Max)):
                    m = z3.FreshReal()
                    s.add(z3.Or([m == x for x in terms]))
                    s.add(z3.And([(m <= x) if isinstance(node, exp.Min) else (m >= x) for x in terms]))
                    return m
                raise _Unsupported
            if isinstance(node, exp.Column):
                r = qi.resolve(node)
                if r is None:
                    raise _Unsupported
                oid, c = r
                t = qi.occ_by_id(oid).table
                if oid not in outer:
                    raise _Unsupported
                return val(t, c, outer[oid])
            raise _Unsupported

        ops = {exp.GT: lambda a, b: a > b, exp.GTE: lambda a, b: a >= b, exp.LT: lambda a, b: a < b,
               exp.LTE: lambda a, b: a <= b, exp.EQ: lambda a, b: a == b, exp.NEQ: lambda a, b: a != b}
        if type(A) not in ops:
            raise _Unsupported

        def truth(outer, members):
            return ops[type(A)](tr(A.this, outer, members), tr(A.expression, outer, members))

        def lhs_rhs(outer, members):
            return tr(A.this, outer, members), tr(A.expression, outer, members)

        goal, goal_dec = [], []
        passes = []
        if mode == "ctx":
            sq = _agg_subquery(A)
            # members of the subquery are the rowids of the aggregated (inner) occurrence; outer columns of A come
            # from the outer row; inner columns inside the aggregate from each member.
            ctxs = [tuple(sorted(r["corr"])) for r in rows]
            for r in rows:
                outer = {o: rid for o, rid in r["rowids"].items()}
                try:
                    own = truth(outer, r["corr"])
                except _Null:
                    continue
                passes.append(own)
                alts = []
                if sorted(r["decor"]) != sorted(r["corr"]):
                    alts.append(r["decor"])
                for c in dict.fromkeys(ctxs):
                    if c and list(c) != sorted(r["corr"]):
                        alts.append(list(c))
                for j, m in enumerate(alts[:4]):
                    try:
                        d = own != truth(outer, m)
                    except _Null:
                        continue
                    goal.append(d)
                    if j == 0 and m is r["decor"]:
                        goal_dec.append(d)
        else:
            gs = []
            for r in rows:
                try:
                    gs.append((truth({}, r["members"]), lhs_rhs({}, r["members"])))
                except _Null:
                    continue
            if mode == "mixed":
                goal = [z3.And(a, z3.Not(b)) for i, (a, _) in enumerate(gs) for j, (b, _) in enumerate(gs) if i != j]
            else:
                goal = [x == y for _, (x, y) in gs]
            passes = [a for a, _ in gs]
        if not goal:
            return "fail", None
        s.add(z3.Or(goal))
        lits = []
        for i, (k, lo, hi) in enumerate(prefs):
            b = z3.Bool(f"np{i}")
            s.add(z3.Implies(b, k == self.rng.randint(lo, max(lo, hi))))
            lits.append(b)
        pb, gd = z3.Bool("passes"), z3.Bool("decor")
        if passes:
            s.add(z3.Implies(pb, z3.Or(passes)))
        if goal_dec:
            s.add(z3.Implies(gd, z3.Or(goal_dec)))     # prefer: own context vs the decorrelated one
        assume = [gd, pb] + lits
        res = s.check(*assume)
        for _ in range(8):
            if res != z3.unsat or not assume:
                break
            core = set(map(str, s.unsat_core()))
            if not core:
                assume = []
            else:
                assume = [b for b in assume if str(b) not in core]
            res = s.check(*assume)
        if res == z3.unsat:
            return "unsat", None
        if res != z3.sat:
            return "fail", None
        m = s.model()
        new = copy.deepcopy(db)
        for (t, c, i), (k, f) in X.items():
            x = m.eval(k, model_completion=True).as_long()
            col = S.column(t, c)
            if col.type == INT:
                v = x
            elif col.type == DEC:
                v = (decimal.Decimal(x) / f).quantize(decimal.Decimal(1).scaleb(-self._scale(t, c)))
            else:
                v = x / f
            new[t][i][c] = v
        if S.violations(new):
            return "fail", None
        return "sat", new


class _Unsupported(Exception):
    pass


class _Null(Exception):
    pass

