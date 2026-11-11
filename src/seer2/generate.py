"""Constraint-valid instance construction: Z3 witness insertion (3-valued) and constraint-preserving mutations.

Witness insertion (ported from seer_batch.Witness and extended): one fresh row per table occurrence of one query plus
optional hidden FK parents. Non-key columns are pool indices; key columns are integer codes of their key domain (equal
codes = equal values across FK-connected columns). Every atom is a pair of Z3 Booleans (is TRUE, is FALSE), so WHERE /
ON / HAVING follow SQL's Kleene logic and NULL is a first-class value. Truth tables of atoms over pool values are
computed by the target engine (AtomEval), never re-implemented. Hard constraints: PK freshness against the base instance and between
occurrences, UNIQUE (NULLs distinct), NOT NULL, composite FK as tuple membership with MATCH SIMPLE (any NULL passes),
column-vs-column CHECK; single-column CHECKs are applied to the pools. The result is re-checked by
Schema.violations before it is used (runner assertion). EXISTS / IN over an uncorrelated-join scope are evaluated over
the new witness row and every existing base row of that table (substituted into the same Z3 terms), not only the
witness row. Aggregate atoms (scalar aggregate subqueries, HAVING) are handled by aggwit.AggWitness, which builds
multi-row, multi-group instances and solves linear arithmetic over SUM / COUNT / AVG / MIN / MAX.

Mutations (all keep the instance valid; asserted after each change): clone with fresh keys (optionally with its
dependants), cascade delete, non-key update (pool value or a copy of another row's value -> ties), FK re-point,
set-to-NULL on a nullable column.
"""
from __future__ import annotations

import copy
import datetime as dt
import decimal
import random

import pyarrow as pa
import z3
from sqlglot import exp

from .pools import NULLK, Pools
from .query import CMP, QInfo, atoms_of
from .schema import BOOL, DATE, DEC, FLOAT, INT, STR, TS, Schema

Z3_TIMEOUT_MS = 2000
MAX_OCC = 32
MAX_BASE_ROWS = 24          # base rows an EXISTS / IN scope is evaluated on


def arrow_type(ty, param=None):
    return {INT: pa.int64(), DEC: pa.decimal128(38, (param or (18, 2))[1]), FLOAT: pa.float64(), STR: pa.string(),
            DATE: pa.date32(), BOOL: pa.bool_(), TS: pa.timestamp("us")}[ty]


_PG_TYPE = {INT: "BIGINT", FLOAT: "DOUBLE PRECISION", STR: "TEXT", DATE: "DATE", BOOL: "BOOLEAN", TS: "TIMESTAMP"}


class AtomEval:
    """Truth values ('T' / 'F' / 'N') of atom templates over candidate values, evaluated by the target engine: the
    values are loaded into a one- or two-column table of the backend and the atom, rendered in the queries' dialect,
    runs there (transpiled only when the dialect differs from the engine, exactly as the queries themselves are), so
    engine-specific semantics (integer division, SQLite type affinity and text dates, PostgreSQL numeric / date casts,
    collations) are the backend's own, not DuckDB's."""

    def __init__(self, engine="duckdb", dialect=None, dsn=None):
        if engine == "mysql":       # candidate atoms only steer generation; MySQL itself executes every instance
            engine = "duckdb"
        self.engine = engine
        self.dialect = dialect or engine
        self.cache = {}
        if engine == "duckdb":
            # Importing DuckDB creates its default connection/thread pool. SQLite
            # and PostgreSQL atom evaluation must not allocate that unused pool.
            import duckdb
            self.con = duckdb.connect(config={"threads": 1})
        elif engine == "sqlite":
            import sqlite3
            self.con = sqlite3.connect(":memory:")
        elif engine == "postgres":
            import os

            import psycopg
            self.con = psycopg.connect(dsn or os.environ.get("SEER2_PG_DSN",
                                                             "host=127.0.0.1 port=55917 dbname=postgres user=probe"))
        else:
            raise ValueError(engine)

    def _rows(self, sql, tables):
        """tables: [(name, [(i, value)], type, param)]; returns the rows of sql (in self.dialect) over them."""
        from .backends import _sqlite_val, transpile
        q = sql if self.dialect == self.engine else transpile(sql, self.dialect, self.engine)
        if self.engine == "duckdb":
            for name, rows, ty, param in tables:
                k = name[1]
                self.con.register(name, pa.table({f"i{k}": pa.array([i for i, _ in rows], pa.int64()),
                                                  f"x{k}": pa.array([v for _, v in rows], arrow_type(ty, param))}))
            return self.con.execute(q).fetchall()
        if self.engine == "sqlite":
            cur = self.con.cursor()
            for name, rows, ty, param in tables:
                k = name[1]
                cur.execute(f"DROP TABLE IF EXISTS {name}")
                cur.execute(f"CREATE TEMP TABLE {name} (i{k} INTEGER, x{k})")
                cur.executemany(f"INSERT INTO {name} VALUES (?, ?)", [(i, _sqlite_val(v)) for i, v in rows])
            return cur.execute(q).fetchall()
        cur = self.con.cursor()
        try:
            for name, rows, ty, param in tables:
                k = name[1]
                pty = f"NUMERIC({(param or (18, 2))[0]}, {(param or (18, 2))[1]})" if ty == DEC else _PG_TYPE[ty]
                cur.execute(f"CREATE TEMP TABLE {name} (i{k} BIGINT, x{k} {pty}) ON COMMIT DROP")
                cur.executemany(f"INSERT INTO {name} VALUES (%s, %s)", rows)
            cur.execute(q)
            return cur.fetchall()
        finally:
            self.con.rollback()

    def unary(self, sql, vals, ty, param=None):
        key = ("u", sql, ty)
        memo = self.cache.setdefault(key, {})
        if sql is None:
            return None
        # dedupe by (value, type): 2, 2.0 and True compare equal but are distinct pool values
        missing = [v for v, _ in dict.fromkeys((v, type(v)) for v in vals) if (v, type(v)) not in memo]
        if missing:
            try:
                res = self._rows(f"SELECT i0, CASE WHEN ({sql}) IS TRUE THEN 'T' WHEN ({sql}) IS FALSE THEN 'F' "
                                 f"ELSE 'N' END FROM p0", [("p0", list(enumerate(missing)), ty, param)])
                for i, t in res:
                    memo[(missing[i], type(missing[i]))] = t
            except Exception:
                return None
        if any((v, type(v)) not in memo for v in vals):
            return None
        return [memo[(v, type(v))] for v in vals]

    def binary(self, sql, vals0, ty0, p0, vals1, ty1, p1):
        if sql is None:
            return None
        key = ("b", sql, ty0, ty1, tuple(map(repr, vals0)), tuple(map(repr, vals1)))
        if key in self.cache:
            return self.cache[key]
        try:
            res = self._rows(f"SELECT i0, i1, CASE WHEN ({sql}) IS TRUE THEN 'T' WHEN ({sql}) IS FALSE THEN 'F' "
                             f"ELSE 'N' END FROM p0, p1", [("p0", list(enumerate(vals0)), ty0, p0),
                                                          ("p1", list(enumerate(vals1)), ty1, p1)])
            out = {(i, j): t for i, j, t in res}
        except Exception:
            out = None
        self.cache[key] = out
        return out


def template(node, qi: QInfo, refs):
    """Atom SQL with its resolved columns replaced by x0, x1 (order of refs); None if a column of the atom does
    not resolve to one of refs (outer reference, alias of a derived column): the atom is then not a function of
    refs alone and gets no witness."""
    e = node.copy()
    orig = list(node.find_all(exp.Column))
    for o, c in zip(orig, list(e.find_all(exp.Column))):
        r = qi.resolve(o)
        if r not in refs:
            return None
        c.replace(exp.column(f"x{refs.index(r)}"))
    return e.sql(dialect=qi.dialect)


class Witness:
    def __init__(self, schema: Schema, pools: Pools, aeval: AtomEval, seed=0):
        self.schema, self.pools, self.aeval = schema, pools, aeval
        self.seed = seed
        self.stats = {"sat": 0, "unsat": 0, "unknown": 0, "gap": 0}
        self._ncheck = 0

    # ------------------------------------------------------------------ solving
    def solve(self, qi: QInfo, db, force_atom=None, force_formula=None, null_col=None, omit=None, mode="strict",
              pol=None, extra=None, disjoint=False):
        """Returns ("sat", rows, info) | ("unsat", None, None) | ("fail", None, None).
        force_atom: (atom key, 'T'|'F'|'N'); force_formula: {fid: bool}; null_col: (table, col); omit: set of oids
        whose rows are not inserted (EXISTS / IN over a scope whose occurrences are all omitted is FALSE);
        pol: {fid: bool | None} default polarity of asserted formulas (None = unconstrained);
        extra: [(template over x0, (table, col), 'T'|'F'|'N')] atoms of another query evaluated on this witness row;
        disjoint: FKs of the new rows may not point to rows of the base instance (a separate group / unit)."""
        if not qi.ok:
            return "fail", None, None
        S = self.schema
        omit = set(omit or ())
        s = z3.Solver()
        s.set("timeout", Z3_TIMEOUT_MS)
        s.set("random_seed", self.seed)
        occ = [(o.oid, o.table) for o in qi.occ]
        hidden = set()
        i = 0
        while i < len(occ):
            oid, t = occ[i]
            if oid not in omit:
                for fk in S.tables[t].fks:
                    if fk.deferred or len(occ) >= MAX_OCC:
                        continue
                    h = f"h{len(occ)}"
                    occ.append((h, fk.ref)); hidden.add(h)
            i += 1
        V = {}
        soft = []                  # (var, #non-NULL pool entries) of non-key columns: random value preferences
        for oid, t in occ:
            for c in S.tables[t].columns:
                v = z3.Int(f"{oid}.{c.name}")
                V[(oid, c.name)] = v
                if S.is_key(t, c.name):
                    d = self.pools.domain_of(t, c.name)
                    lo, flo, hi = d.bounds()
                    rng = z3.And(v >= lo, v < hi) if d.is_int else \
                        z3.Or(z3.And(v >= 0, v < len(d.values)), z3.And(v >= flo, v < hi))
                    s.add(z3.Or(rng, v == NULLK) if c.nullable else rng)
                else:
                    n = len(self.pools.col[(t, c.name)])
                    s.add(v >= 0, v < n)
                    soft.append((v, n - 1 if c.nullable else n))
        ins = [(o, t) for o, t in occ if o not in omit]

        def code(t, c, val):
            return self.pools.domain_of(t, c).encode(val)

        def isnull(oid, t, c):
            col = S.column(t, c)
            if not col.nullable:
                return z3.BoolVal(False)
            if S.is_key(t, c):
                return V[(oid, c)] == NULLK
            return V[(oid, c)] == len(self.pools.col[(t, c)]) - 1      # NULL is the last pool entry

        # PK / UNIQUE against the base instance and between occurrences
        for oid, t in ins:
            tb = S.tables[t]
            for key in ([tb.pk] if tb.pk else []) + tb.uniques:
                for r in db.get(t, []):
                    if any(r[c] is None for c in key):
                        continue
                    diff = [V[(oid, c)] != code(t, c, r[c]) for c in key]
                    diff += [isnull(oid, t, c) for c in key if c not in tb.pk]
                    s.add(z3.Or(diff))
        for a, (o1, t1) in enumerate(ins):
            for o2, t2 in ins[a + 1:]:
                if t1 != t2:
                    continue
                tb = S.tables[t1]
                same_row = z3.And([V[(o1, c.name)] == V[(o2, c.name)] for c in tb.columns])
                if tb.pk:
                    s.add(z3.Implies(z3.And([V[(o1, c)] == V[(o2, c)] for c in tb.pk]), same_row))
                for u in tb.uniques:
                    eq = z3.And([z3.And(V[(o1, c)] == V[(o2, c)], z3.Not(isnull(o1, t1, c))) for c in u])
                    s.add(z3.Implies(eq, same_row))
        # FK membership (composite FK as tuple, MATCH SIMPLE)
        inserted_if = {}
        for oid, t in occ:
            inserted_if[oid] = z3.BoolVal(oid not in hidden) if oid not in omit else z3.BoolVal(False)
        for oid, t in ins:
            for fk in S.tables[t].fks:
                opts = []
                for r in ([] if disjoint else db.get(fk.ref, [])):
                    opts.append(z3.And([V[(oid, fc)] == code(fk.ref, pc, r[pc]) for fc, pc in zip(fk.cols, fk.ref_cols)]))
                for po, pt in ins:
                    if pt == fk.ref and po != oid:
                        opts.append(z3.And([V[(oid, fc)] == V[(po, pc)] for fc, pc in zip(fk.cols, fk.ref_cols)]))
                nullable = [isnull(oid, t, c) for c in fk.cols if S.column(t, c).nullable]
                opts += nullable
                if oid in hidden:
                    s.add(z3.Or(opts) if opts else z3.BoolVal(False))   # hidden rows may become inserted
                else:
                    s.add(z3.Or(opts) if opts else z3.BoolVal(False))
        # column-vs-column CHECK
        for oid, t in ins:
            for ch in S.tables[t].checks:
                if len(ch.cols) == 2:
                    refs = [(oid, ch.cols[0]), (oid, ch.cols[1])]
                    tf = self._binary_atom(ch.expr.copy(), refs, t, t, ch.cols, raw=True, V=V)
                    if tf is not None:
                        s.add(z3.Not(tf[1]))

        # ------------------------------------------------ formulas (Kleene)
        realised = {}
        self._fresh = 0
        self._noaux = False

        def free():
            self._fresh += 1
            a, b = z3.Bool(f"fT{self._fresh}"), z3.Bool(f"fF{self._fresh}")
            s.add(z3.Not(z3.And(a, b)))
            return a, b

        def pair(T, F):
            self._fresh += 1
            a, b = z3.Bool(f"aT{self._fresh}"), z3.Bool(f"aF{self._fresh}")
            s.add(a == T, b == F)
            return a, b

        def pair(T, F):
            if self._noaux:
                return T, F
            self._fresh += 1
            a, b = z3.Bool(f"aT{self._fresh}"), z3.Bool(f"aF{self._fresh}")
            s.add(a == T, b == F)
            return a, b

        def base_substs(sc):
            """For a single-occurrence predicate scope: substitutions mapping the occurrence's variables to the values
            of each row of the base instance (so EXISTS / IN see every row, not only the new witness row)."""
            if len(sc.occs) != 1 or sc.node.args.get("joins"):
                return []
            oi = sc.occs[0]
            t = qi.occ_by_id(oi).table
            out = []
            for r in db.get(t, [])[:MAX_BASE_ROWS]:
                sub = []
                for c in S.tables[t].columns:
                    if S.is_key(t, c.name):
                        x = NULLK if r[c.name] is None else code(t, c.name, r[c.name])
                    else:
                        pool = self.pools.col[(t, c.name)]
                        try:
                            x = next(i for i, y in enumerate(pool) if y == r[c.name] and type(y) is type(r[c.name]))
                        except StopIteration:
                            sub = None
                            break
                    sub.append((V[(oi, c.name)], z3.IntVal(x)))
                if sub is not None:
                    out.append(sub)
            return out

        def scope_where(sel, extra_atom=None):
            """[(W, X)] per candidate inner row: W = TRUE-part of the predicate scope's WHERE / ON on that row, X =
            the TRUE / FALSE parts of extra_atom (the IN comparison) on that row. Candidates: the new witness row (unless
            its occurrences are omitted) and, for a single-occurrence scope, every row of the base instance."""
            sc = qi.scopes.get(id(sel))
            if sc is None:
                return None
            w = sel.args.get("where")
            prev, self._noaux = self._noaux, True
            try:
                parts = [z3.BoolVal(True)]
                if w is not None:
                    parts.append(encf(w.this)[0])
                for j in sel.args.get("joins") or []:
                    if j.args.get("on") is not None:
                        parts.append(encf(j.args["on"])[0])
                W = z3.And(parts)
                X = extra_atom() if extra_atom is not None else None
            finally:
                self._noaux = prev
            if extra_atom is not None and X is None:
                return None
            out = []
            if not (sc.occs and all(o in omit for o in sc.occs)):
                out.append((W, X))
            for sub in base_substs(sc):
                out.append((z3.substitute(W, *sub),
                            None if X is None else (z3.substitute(X[0], *sub), z3.substitute(X[1], *sub))))
            return out

        def encf(node):
            if isinstance(node, exp.Paren):
                return encf(node.this)
            if isinstance(node, exp.And):
                a, b = encf(node.this), encf(node.expression)
                return z3.And(a[0], b[0]), z3.Or(a[1], b[1])
            if isinstance(node, exp.Or):
                a, b = encf(node.this), encf(node.expression)
                return z3.Or(a[0], b[0]), z3.And(a[1], b[1])
            if isinstance(node, exp.Not):
                a = encf(node.this)
                return a[1], a[0]
            if isinstance(node, exp.Boolean):
                return z3.BoolVal(node.this), z3.BoolVal(not node.this)
            r = atom(node)
            key = qi.norm_sql(node)
            realised.setdefault(key, []).append(r)
            return r

        def atom(node):
            if isinstance(node, exp.Exists):
                cs = scope_where(node.this)
                if cs is None:
                    return free()
                T = z3.Or([w for w, _ in cs]) if cs else z3.BoolVal(False)
                return pair(T, z3.Not(T))
            if isinstance(node, exp.In) and node.args.get("query") is not None:
                sel = node.args["query"].this
                if not isinstance(sel, exp.Select) or not sel.expressions:
                    return free()
                proj = sel.expressions[0]
                proj = proj.this if isinstance(proj, exp.Alias) else proj
                lhs = node.this
                if not (isinstance(lhs, exp.Column) and isinstance(proj, exp.Column)):
                    return free()
                ra, rb = qi.resolve(lhs), qi.resolve(proj)
                if ra is None or rb is None:
                    return free()
                eq = exp.EQ(this=lhs, expression=proj)
                cs = scope_where(sel, extra_atom=lambda: eq_atom(eq, [ra, rb]))
                if cs is None:
                    return free()
                T = z3.Or([z3.And(w, x[0]) for w, x in cs]) if cs else z3.BoolVal(False)
                N = z3.And(z3.Not(T), z3.Or([z3.And(w, z3.Not(x[0]), z3.Not(x[1])) for w, x in cs])) if cs \
                    else z3.BoolVal(False)
                return pair(T, z3.And(z3.Not(T), z3.Not(N)))
            if not isinstance(node, CMP):
                return free()
            refs = qi.refs(node)
            if not refs or len(refs) > 2:
                return free()
            r = eq_atom(node, refs)
            return pair(*r) if r is not None else free()

        def eq_atom(node, refs):
            tabs = [qi.occ_by_id(o).table for o, _ in refs]
            keys = [S.is_key(t, c) for t, (_, c) in zip(tabs, refs)]
            if any(o in omit for o, _ in refs):
                pass
            if len(refs) == 2 and all(keys):
                if isinstance(node, (exp.EQ, exp.NEQ)) and isinstance(node.this, exp.Column) \
                        and isinstance(node.expression, exp.Column) \
                        and self.pools.domain_of(tabs[0], refs[0][1]) is self.pools.domain_of(tabs[1], refs[1][1]):
                    a, b = V[refs[0]], V[refs[1]]
                    nn = z3.And(z3.Not(isnull(refs[0][0], tabs[0], refs[0][1])),
                                z3.Not(isnull(refs[1][0], tabs[1], refs[1][1])))
                    e_ = z3.And(nn, a == b)
                    ne = z3.And(nn, a != b)
                    return (e_, ne) if isinstance(node, exp.EQ) else (ne, e_)
                return None
            if len(refs) == 1:
                (oid, c), t = refs[0], tabs[0]
                sql = template(node, qi, refs)
                col = S.column(t, c)
                v = V[(oid, c)]
                if keys[0]:
                    d = self.pools.domain_of(t, c)
                    cand = set(d.lits)
                    for tt, cc in d.cols:
                        cand.update(d.encode(r[cc]) for r in db.get(tt, []))
                    cand.discard(NULLK)
                    cand = sorted(cand)
                    rep = d.fresh_rep()
                    vals = [d.decode(x) for x in cand] + [d.decode(rep)] + [None]
                    tv = self.aeval.unary(sql, vals, d.type, d.param)
                    if tv is None:
                        return None
                    fresh_reg = v >= rep
                    s.add(z3.Or([v == x for x in cand] + [fresh_reg, v == NULLK]))
                    T = z3.Or([v == x for x, t_ in zip(cand, tv) if t_ == "T"] + ([fresh_reg] if tv[-2] == "T" else [])
                              + ([v == NULLK] if tv[-1] == "T" else []))
                    F = z3.Or([v == x for x, t_ in zip(cand, tv) if t_ == "F"] + ([fresh_reg] if tv[-2] == "F" else [])
                              + ([v == NULLK] if tv[-1] == "F" else []))
                    return T, F
                vals = self.pools.col[(t, c)]
                tv = self.aeval.unary(sql, vals, col.type, col.param)
                if tv is None:
                    return None
                T = z3.Or([v == i for i, x in enumerate(tv) if x == "T"])
                F = z3.Or([v == i for i, x in enumerate(tv) if x == "F"])
                return T, F
            if not any(keys):
                return self._binary_atom(node, refs, tabs[0], tabs[1], [refs[0][1], refs[1][1]], qi=qi, V=V)
            return None

        F_ = {}
        for f in qi.formulas:
            F_[f.fid] = encf(f.node)
        asserted = [f for f in qi.formulas if f.kind in ("where", "on", "having")
                    and qi.scopes[f.sid].role in ("main", "derived", "scalar")]
        holds = set()
        if force_atom is not None:
            key, p = force_atom
            if key not in realised:
                return "fail", None, None
            for a, b in realised[key]:
                s.add({"T": a, "F": b, "N": z3.And(z3.Not(a), z3.Not(b))}[p])
            avars = realised[key]
            for f in asserted:
                if any(key == qi.norm_sql(x) for x in atoms_of(f.node)) or \
                        any(key == qi.norm_sql(x) for x in f.node.find_all(*CMP)):
                    holds.add(f.fid)
            if mode == "strict":
                for fid in holds:
                    ft = F_[fid][0]
                    on = z3.substitute(ft, *[(x, z3.BoolVal(True)) for x, _ in avars],
                                       *[(y, z3.BoolVal(False)) for _, y in avars])
                    s.add(on)                         # the atom decides its formula (MC/DC style)
                    s.add(ft if p == "T" else z3.Not(ft))
        for tmpl, (t, c), p in extra or ():
            oids = [o for o, tt in ins if tt == t and o not in hidden and qi.scopes[qi.occ_by_id(o).sid].role
                    in ("main", "derived")]
            if not oids:
                return "fail", None, None
            col = S.column(t, c)
            tv = self.aeval.unary(tmpl, self.pools.col[(t, c)], col.type, col.param)
            if tv is None:
                return "fail", None, None
            s.add(z3.Or([V[(oids[0], c)] == i for i, x in enumerate(tv) if x == p] or [z3.BoolVal(False)]))
        if null_col is not None:
            t, c = null_col
            if not S.column(t, c).nullable:
                return "unsat", None, None
            opts = [isnull(o, tt, c) for o, tt in ins if tt == t]
            if not opts:
                return "fail", None, None
            s.add(z3.Or(opts))
            if mode != "strict":
                for f in asserted:
                    if any(qi.column_table(x) == (t, c) for x in f.node.find_all(exp.Column)):
                        holds.add(f.fid)
        for fid, val in (force_formula or {}).items():
            if fid in F_:
                s.add(F_[fid][0] if val else z3.Not(F_[fid][0]))
                holds.add(fid)
        if mode != "bare":
            for f in asserted:
                if f.fid in holds:
                    continue
                if f.sid in {qi.occ_by_id(o).sid for o in omit}:
                    continue
                want = True if pol is None else pol.get(f.fid, True)
                if want is not None:
                    s.add(F_[f.fid][0] if want else z3.Not(F_[f.fid][0]))
        res = self._check(s, soft)
        if res == z3.unsat:
            self.stats["unsat"] += 1
            return "unsat", None, None
        if res != z3.sat:
            self.stats["unknown"] += 1
            return "fail", None, None
        m = s.model()
        vals = {}
        for oid, t in occ:
            r = {}
            for c in S.tables[t].columns:
                x = m.eval(V[(oid, c.name)], model_completion=True).as_long()
                if S.is_key(t, c.name):
                    r[c.name] = self.pools.domain_of(t, c.name).decode(x)
                else:
                    r[c.name] = self.pools.col[(t, c.name)][x]
            vals[oid] = (t, r)
        keep = {o for o, _ in occ if o not in hidden and o not in omit}
        changed = True
        while changed:
            changed = False
            for oid in list(keep):
                t, r = vals[oid]
                for fk in S.tables[t].fks:
                    tup = tuple(r[c] for c in fk.cols)
                    if any(x is None for x in tup):
                        continue
                    for h in hidden - keep:
                        pt, pr = vals[h]
                        if pt == fk.ref and tuple(pr[c] for c in fk.ref_cols) == tup:
                            keep.add(h); changed = True
                            break
        rows, seen = {}, set()
        for oid, t in occ:
            if oid not in keep:
                continue
            r = vals[oid][1]
            pk = S.tables[t].pk or S.tables[t].colnames
            k = (t, tuple(r[c] for c in pk))
            if k in seen:
                continue
            seen.add(k)
            rows.setdefault(t, []).append(dict(r))
        new = {t: [dict(r) for r in db.get(t, [])] + rows.get(t, []) for t in S.order}
        if S.violations(new):
            self.stats["gap"] += 1        # encoding gap (e.g. a key CHECK); never used
            return "fail", None, None
        got = {k: ["T" if z3.is_true(m.eval(a, model_completion=True)) else
                   "F" if z3.is_true(m.eval(b, model_completion=True)) else "N" for a, b in v]
               for k, v in realised.items()}
        self.stats["sat"] += 1
        return "sat", new, {"atoms": got, "rows": rows,
                            "oids": {oid: vals[oid] for oid in keep}}

    def _check(self, s, soft):
        """check() under random value preferences for the unconstrained part of the witness (assumption literals;
        preferences in the UNSAT core are dropped and the check repeated). Without this the solver tends to put the
        first pool value everywhere, which makes arithmetic degenerate (e.g. l_discount = 1 gives revenue 0, so a
        doubled join row changes nothing)."""
        self._ncheck += 1
        rng = random.Random(f"{self.seed}-{self._ncheck}")
        lits = {}
        for i, (v, n) in enumerate(soft):
            if n > 1 and rng.random() < 0.95:
                b = z3.Bool(f"pref{i}")
                s.add(z3.Implies(b, v == rng.randrange(n)))
                lits[b] = None
        s.set("core.minimize", True)
        for _ in range(8):
            if not lits:
                break
            r = s.check(*lits)
            if r == z3.sat:
                return r
            if r != z3.unsat:
                break
            core = s.unsat_core()
            if not len(core):
                break
            for b in core:
                lits.pop(b, None)
            self.stats["pref_drop"] = self.stats.get("pref_drop", 0) + len(core)
        return s.check()

    def _binary_atom(self, node, refs, t0, t1, cols, raw=False, qi=None, V=None):
        if raw:
            e = node
            for c in list(e.find_all(exp.Column)):
                c.replace(exp.column(f"x{cols.index(c.name.lower())}"))
            sql = e.sql(dialect="duckdb")
        else:
            sql = template(node, qi, refs)
        c0, c1 = self.schema.column(t0, cols[0]), self.schema.column(t1, cols[1])
        p0, p1 = self.pools.col[(t0, cols[0])], self.pools.col[(t1, cols[1])]
        tab = self.aeval.binary(sql, p0, c0.type, c0.param, p1, c1.type, c1.param)
        if tab is None:
            return None
        a, b = V[refs[0]], V[refs[1]]
        T = z3.Or([z3.And(a == i, b == j) for (i, j), x in tab.items() if x == "T"])
        F = z3.Or([z3.And(a == i, b == j) for (i, j), x in tab.items() if x == "F"])
        return T, F


# ====================================================================== mutations
class Mutator:
    def __init__(self, schema: Schema, pools: Pools, rng: random.Random):
        self.S, self.pools, self.rng = schema, pools, rng

    def _pkey(self, t, r):
        tb = self.S.tables[t]
        return tuple(r[c] for c in (tb.pk or tb.colnames))

    def mint(self, db, t, c):
        """A value for key column t.c that is not used by any column of its domain in db."""
        d = self.pools.domain_of(t, c)
        used = {r[cc] for tt, cc in d.cols for r in db.get(tt, [])}
        if d.is_int:
            ints = [x for x in used if isinstance(x, int)] + [x for x in d.lits]
            v = max([0] + ints) + self.rng.randint(1, 3)
            return v
        k = d.bounds()[1]
        while True:
            v = d.decode(k)
            if v not in used:
                return v
            k += 1

    def fresh_copy(self, db, t, base, depth=0):
        """Copy of row `base` of t with a PK (and UNIQUE values) not present in db; None if impossible."""
        S = self.S
        tb = S.tables[t]
        new = dict(base)
        fkcols = {c for fk in tb.fks for c in fk.cols}
        keys = {self._pkey(t, r) for r in db.get(t, [])}
        free = [c for c in tb.pk if c not in fkcols]
        if tb.pk and free:
            new[free[0]] = self.mint(db, t, free[0])
        elif tb.pk:
            # PK made only of FK columns: choose another parent combination
            ok = False
            for _ in range(30):
                cand = dict(new)
                for fk in tb.fks:
                    if not set(fk.cols) & set(tb.pk):
                        continue
                    ps = db.get(fk.ref, [])
                    if not ps:
                        break
                    p = self.rng.choice(ps)
                    for fc, pc in zip(fk.cols, fk.ref_cols):
                        cand[fc] = p[pc]
                if self._pkey(t, cand) not in keys:
                    new, ok = cand, True
                    break
            if not ok and depth < 2:
                fk = self.rng.choice([f for f in tb.fks if set(f.cols) & set(tb.pk)])
                ps = db.get(fk.ref, [])
                if not ps:
                    return None
                np_ = self.fresh_copy(db, fk.ref, self.rng.choice(ps), depth + 1)
                if np_ is None:
                    return None
                db[fk.ref].append(np_)
                for fc, pc in zip(fk.cols, fk.ref_cols):
                    new[fc] = np_[pc]
                if self._pkey(t, new) in keys:
                    return None
            elif not ok:
                return None
        for u in tb.uniques:
            for c in u:
                if c in fkcols:
                    continue
                if S.column(t, c).nullable and self.rng.random() < 0.3:
                    new[c] = None
                else:
                    new[c] = self.mint(db, t, c)
        return new

    def clone(self, db, t=None, row=None, deep=None, vary=None):
        S = self.S
        ts = [x for x in S.order if db.get(x)]
        if not ts:
            return False
        t = t or self.rng.choice(ts)
        if not db.get(t):
            return False
        row = row if row is not None else self.rng.choice(db[t])
        snap = copy.deepcopy(db)
        new = self.fresh_copy(db, t, row)
        if new is None:
            db.clear(); db.update(snap)
            return False
        if vary:
            for c in vary:
                pool = [v for v in self.pools.col[(t, c)] if v != new[c]]
                if pool:
                    new[c] = self.rng.choice(pool)
        db[t].append(new)
        deep = self.rng.random() < 0.5 if deep is None else deep
        if deep:
            # one copy per original dependant row, with every FK of it that references `row` redirected to `new`
            # (a dependant referencing `row` through several FKs, e.g. 22 player columns of a match, is copied once;
            # iterating FK by FK over the growing table would re-copy the copies, doubling it per FK)
            fks_of = {}
            for child, fk in S.children[t]:
                if child != t:
                    fks_of.setdefault(child, []).append(fk)
            for child, fks in fks_of.items():
                rows = list(db.get(child, []))
                keys = {self._pkey(child, x) for x in rows}
                uks = [(u, {tuple(x[c] for c in u) for x in rows}) for u in S.tables[child].uniques]
                for cr in rows:
                    hit = [fk for fk in fks if tuple(cr[c] for c in fk.cols) == tuple(row[c] for c in fk.ref_cols)]
                    if not hit:
                        continue
                    nc = dict(cr)
                    for fk in hit:
                        for fc, pc in zip(fk.cols, fk.ref_cols):
                            nc[fc] = new[pc]
                    if self._pkey(child, nc) in keys or any(tuple(nc[c] for c in u) in vs for u, vs in uks):
                        nc = self.fresh_copy(db, child, nc)
                        if nc is None:
                            continue
                    db[child].append(nc)
                    keys.add(self._pkey(child, nc))
                    for u, vs in uks:
                        vs.add(tuple(nc[c] for c in u))
        if S.violations(db):
            db.clear(); db.update(snap)
            return False
        return True

    def cascade_delete(self, db, t=None, row=None):
        S = self.S
        ts = [x for x in S.order if db.get(x)]
        if not ts:
            return False
        t = t or self.rng.choice(ts)
        row = row if row is not None else self.rng.choice(db[t])
        stack = [(t, row)]
        while stack:
            t, row = stack.pop()
            if not any(r is row for r in db[t]):
                continue
            db[t] = [r for r in db[t] if r is not row]
            for child, fk in S.children[t]:
                key = tuple(row[c] for c in fk.ref_cols)
                for r in list(db[child]):
                    if tuple(r[c] for c in fk.cols) == key:
                        nullable = all(S.column(child, c).nullable for c in fk.cols)
                        if nullable and self.rng.random() < 0.5:
                            for c in fk.cols:
                                r[c] = None          # ON DELETE SET NULL
                        else:
                            stack.append((child, r))
        return True

    def update(self, db, t=None, row=None, col=None):
        S = self.S
        ts = [x for x in S.order if db.get(x) and any(not S.is_key(x, c.name) for c in S.tables[x].columns)]
        if not ts:
            return False
        t = t or self.rng.choice(ts)
        row = row if row is not None else self.rng.choice(db[t])
        cols = [c.name for c in S.tables[t].columns if not S.is_key(t, c.name)]
        c = col or self.rng.choice(cols)
        old = row[c]
        if self.rng.random() < 0.5 and len(db[t]) > 1:
            row[c] = self.rng.choice(db[t])[c]          # tie with another row
        else:
            row[c] = self.rng.choice(self.pools.col[(t, c)])
        if S.violations(db):
            row[c] = old
            return False
        return True

    def set_null(self, db):
        S = self.S
        opts = [(t, c.name) for t in S.order if db.get(t) for c in S.tables[t].columns
                if c.nullable and c.name not in S.tables[t].pk]
        if not opts:
            return False
        t, c = self.rng.choice(opts)
        row = self.rng.choice(db[t])
        old = row[c]
        row[c] = None
        if S.violations(db):
            row[c] = old
            return False
        return True

    def repoint(self, db):
        S = self.S
        opts = [(t, fk) for t in S.order if db.get(t) for fk in S.tables[t].fks if len(db.get(fk.ref, [])) > 1]
        if not opts:
            return False
        t, fk = self.rng.choice(opts)
        row = self.rng.choice(db[t])
        before = copy.deepcopy(db)
        p = self.rng.choice(db[fk.ref])
        old = dict(row)
        for fc, pc in zip(fk.cols, fk.ref_cols):
            row[fc] = p[pc]
        for child, cfk in S.children[t]:       # dependants of row follow its (possibly changed) key
            oldt = tuple(old[rc] for rc in cfk.ref_cols)
            newt = tuple(row[rc] for rc in cfk.ref_cols)
            if oldt == newt:
                continue
            for x in db.get(child, []):
                if x is not row and tuple(x[c] for c in cfk.cols) == oldt:
                    for c, rc in zip(cfk.cols, cfk.ref_cols):
                        x[c] = row[rc]
        if S.violations(db):
            db.clear(); db.update(before)
            return False
        return True

    def random_op(self, db):
        ops = [(self.clone, 0.3), (self.update, 0.3), (self.cascade_delete, 0.1), (self.repoint, 0.15),
               (self.set_null, 0.15)]
        x = self.rng.random()
        for op, w in ops:
            x -= w
            if x <= 0:
                break
        return op.__name__, op(db)


# ====================================================================== target realisation
def _empty(schema):
    return {t: [] for t in schema.order}


def realize(target, qi: QInfo, base, wit: Witness, mut: Mutator):
    """Build one constraint-valid instance for a target. Returns (status, db) with status in
    {"sat", "unsat", "fail"}; "unsat" means every encoding mode was UNSAT (proven under the witness encoding)."""
    S = wit.schema
    kind, args = target.kind, target.args
    modes = ("strict", "loose", "bare")

    def first(**kw):
        sts = []
        for m in modes:
            st, db, info = wit.solve(qi, base, mode=m, **kw)
            if st == "sat":
                return st, db, info
            sts.append(st)
        return ("unsat" if all(x == "unsat" for x in sts) else "fail"), None, None

    if kind == "PRED":
        st, db, _ = first(force_atom=args)
    elif kind == "AGG":
        st, db = wit.agg.realize(qi, *args)
        if st != "sat":
            return st, None
    elif kind == "PREDPAIR":
        ka, pa, tmpl, tc, pb = args
        st, db, _ = first(force_atom=(ka, pa), extra=[(tmpl, tc, pb)])
    elif kind == "FORMULA":
        fid, val = args
        st, db, _ = first(force_formula={fid: val})
    elif kind == "NULLCOL":
        st, db, _ = first(null_col=args)
    elif kind == "EMPTY":
        st, db, _ = first(omit=set(args))
    elif kind in ("DUP", "TIE", "GROUPSIZE"):
        oid = args[0]
        st, db, info = first()
        if st != "sat":
            return st, None
        if oid not in info["oids"]:
            return "fail", None
        t, row = info["oids"][oid]
        row = next((r for r in db[t] if all(r[c] == row[c] for c in (S.tables[t].pk or S.tables[t].colnames))), None)
        if row is None:
            return "fail", None
        if kind == "DUP":
            ok = mut.clone(db, t, row, deep=mut.rng.random() < 0.5)
        elif kind == "TIE":
            ok = True
            for _ in range(args[1]):
                # tie on the ORDER BY key: copy the row, or one of its FK ancestors with its dependants
                cands = [(t, row)]
                for fk in S.tables[t].fks:
                    tup = tuple(row[c] for c in fk.cols)
                    p = next((r for r in db.get(fk.ref, []) if tuple(r[c] for c in fk.ref_cols) == tup), None)
                    if p is not None:
                        cands.append((fk.ref, p))
                tt, rr = mut.rng.choice(cands)
                ok = mut.clone(db, tt, rr, deep=True) and ok
        else:
            ok = True
            for _ in range(max(0, args[1] - 1)):
                ok = mut.clone(db, t, row, deep=False) and ok
        if not ok:
            return "fail", None
        st = "sat"
    else:
        raise ValueError(kind)
    if st == "sat":
        S.check(db)
    return st, db
