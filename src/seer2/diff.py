"""Syntactic differences between members of an unsplit class, mapped to data-shape targets.

Each query is summarised as a set of features (alias-free, column references resolved to base table.column):
  pred      every atom of WHERE / ON / HAVING / CASE in every scope, incl. subqueries and IS [NOT] NULL, tagged with
            its context (filter, on-LEFT/RIGHT/FULL, having, case; sub / scalar for subquery scopes);
  subq      IN / NOT IN / EXISTS / NOT EXISTS / scalar subqueries and the tables they read;
  join      join side/kind per joined table;
  distinct, groupby, agg (function, DISTINCT, argument), order, limit, setop, window, proj.
The symmetric difference of two members' feature sets is mapped to targets realised on the member that carries the
feature:
  PRED(atom, T|F|N)        predicate truth value (NULL only if a referenced column is nullable / outer join / subquery)
  NULLCOL(table, col)      NULL in a column (also subquery-projected columns, aggregate arguments, group keys)
  DUP(oid)                 duplicate of a witness row under fresh keys (same projected / grouping values)
  EMPTY(oids)              no rows for some occurrences (empty subquery result / unmatched outer join / empty group)
  FORMULA(fid, False)      an ON formula false (dangling outer-join row)
  TIE(oid, n)              n+1 rows tied on the ORDER BY key at the LIMIT boundary
  GROUPSIZE(oid, n)        a group of exactly n rows (COUNT / HAVING threshold c-1, c, c+1)
Fallback targets (tier 1: all atoms T/F/N, DUP per occurrence, NULLCOL per nullable referenced column, EMPTY per
subquery scope) are used when the difference yields none or all tier-0 targets are resolved.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from .aggwit import agg_atoms
from .query import QInfo, atoms_of
from .schema import Schema


@dataclass(frozen=True)
class Target:
    kind: str
    q: int
    args: tuple
    tier: int = 0

    @property
    def key(self):
        return (self.kind, self.q, self.args)


def _ctx(qi: QInfo, f):
    role = qi.scopes[f.sid].role
    pre = {"pred": "sub:", "scalar": "scalar:"}.get(role, "")
    if f.kind == "on":
        side = f.side if f.side in ("LEFT", "RIGHT", "FULL") else "INNER"
        return pre + ("filter" if side == "INNER" else "on-" + side)
    if f.kind == "where":
        return pre + "filter"
    return pre + f.kind


def _scope_oids(qi, sid):
    """Occurrences of a scope and of every scope nested in it."""
    node = qi.scopes[sid].node
    out = []
    for s in node.find_all(exp.Select):
        sc = qi.scopes.get(id(s))
        if sc is not None:
            out += sc.occs
    return tuple(sorted(set(out)))


def _nullable_refs(qi: QInfo, node, schema: Schema):
    out = []
    for c in node.find_all(exp.Column):
        r = qi.column_table(c)
        if r is not None and schema.column(*r).nullable and r not in out:
            out.append(r)
    return out


def _grain(qi: QInfo, sc, schema: Schema):
    """Occurrences of a scope whose table is not referenced (as FK parent) by another occurrence of the scope."""
    tabs = [qi.occ_by_id(o).table for o in sc.occs]
    out = []
    for o in sc.occs:
        t = qi.occ_by_id(o).table
        if not any(fk.ref == t for t2 in tabs for fk in schema.tables[t2].fks if t2 != t):
            out.append(o)
    return out or list(sc.occs)


def features(qi: QInfo, q: int, schema: Schema):
    """{feature: [Target, ...]} for query q (index in the batch)."""
    F = {}
    if not qi.ok:
        return F

    def add(feat, *targets):
        F.setdefault(feat, [])
        for t in targets:
            if t not in F[feat]:
                F[feat].append(t)

    for f in qi.formulas:
        ctx = _ctx(qi, f)
        for a in atoms_of(f.node):
            key = qi.norm_sql(a)
            ts = [Target("PRED", q, (key, "T")), Target("PRED", q, (key, "F"))]
            nref = _nullable_refs(qi, a, schema) if a.find(exp.Select) is None else []
            if nref or a.find(exp.Select) is not None or ctx.startswith("on-"):
                ts.append(Target("PRED", q, (key, "N")))
            ts += [Target("NULLCOL", q, r) for r in nref]
            add(("pred", ctx, key), *ts)
    # aggregate-aware targets: attached to the atom's own feature, to every feature of the scalar subquery's
    # atoms (correlation predicates) and to the HAVING / GROUP BY features of its scope
    aggs = agg_atoms(qi)
    agg_t = {k: [Target("AGG", q, (k, m)) for kk, m in aggs if kk == k] for k, _ in aggs}
    for f in qi.formulas:
        for a in atoms_of(f.node):
            key = qi.norm_sql(a)
            if key in agg_t:
                feat = ("pred", _ctx(qi, f), key)
                add(feat, *agg_t[key])
                for sub in a.find_all(exp.Select):
                    for g in qi.formulas:
                        if g.sid == id(sub):
                            for b in atoms_of(g.node):
                                add(("pred", _ctx(qi, g), qi.norm_sql(b)), *agg_t[key])
                if f.kind == "having":
                    g = qi.scopes[f.sid].node.args.get("group")
                    if g is not None:
                        add(("groupby", tuple(sorted(qi.norm_sql(e) for e in g.expressions))), *agg_t[key])
    # subqueries
    for sid, sc in qi.scopes.items():
        if sc.role not in ("pred", "scalar"):
            continue
        oids = _scope_oids(qi, sid)
        tabs = tuple(sorted({qi.occ_by_id(o).table for o in oids}))
        ts = []
        if oids:
            ts.append(Target("EMPTY", q, oids))
            ts += [Target("DUP", q, (o,)) for o in sc.occs]
        if sc.role == "pred" and isinstance(sc.pred, exp.In):
            kind = "NOT IN" if isinstance(sc.pred.parent, exp.Not) else "IN"
            proj = sc.node.expressions[0] if sc.node.expressions else None
            for node in (proj, sc.pred.this):
                if node is not None:
                    ts += [Target("NULLCOL", q, r) for r in _nullable_refs(qi, node, schema)]
        elif sc.role == "pred":
            p = sc.pred
            kind = "NOT EXISTS" if isinstance(p.parent, exp.Not) else "EXISTS"
            w = sc.node.args.get("where")
            if w is not None:
                ts += [Target("NULLCOL", q, r) for r in _nullable_refs(qi, w, schema)]
        else:
            kind = "SCALAR"
        add(("subq", kind, tabs), *ts)
    for sid, sc in qi.scopes.items():
        s = sc.node
        # joins
        for j in s.args.get("joins") or []:
            side = str(j.args.get("side") or j.args.get("kind") or ("INNER" if j.args.get("on") else "CROSS")).upper()
            src = j.this
            if isinstance(src, exp.Table):
                alias = (src.alias or src.name).lower()
                tname = src.name.lower()
            else:
                alias, tname = (src.alias or "").lower(), "derived"
            kind, x = sc.sources.get(alias, (None, None))
            oids = (x,) if kind == "base" else (_scope_oids(qi, id(x)) if kind == "derived" and id(x) in qi.scopes else ())
            ts = []
            if oids:
                ts.append(Target("EMPTY", q, tuple(oids)))
                ts += [Target("DUP", q, (o,)) for o in oids]
            on = j.args.get("on")
            if on is not None:
                fid = next((f.fid for f in qi.formulas if f.node is on), None)
                if fid is not None:
                    ts.append(Target("FORMULA", q, (fid, False)))
                ts += [Target("NULLCOL", q, r) for r in _nullable_refs(qi, on, schema)]
            add(("join", side, tname), *ts)
        dup_scope = [Target("DUP", q, (o,)) for o in sc.occs]
        if s.args.get("distinct"):
            add(("distinct", sc.role), *dup_scope)
        g = s.args.get("group")
        if g is not None:
            keys = tuple(sorted(qi.norm_sql(e) for e in g.expressions))
            ts = list(dup_scope)
            for e in g.expressions:
                ts += [Target("NULLCOL", q, r) for r in _nullable_refs(qi, e, schema)]
            add(("groupby", keys), *ts)
        for agg in s.find_all(exp.AggFunc):
            if id(_owner(agg)) != sid:
                continue
            arg = agg.this
            dist = isinstance(arg, exp.Distinct)
            argsql = "*" if isinstance(arg, exp.Star) or arg is None else qi.norm_sql(arg)
            ts = []
            if sc.occs:
                ts.append(Target("EMPTY", q, _scope_oids(qi, sid)))
            if arg is not None and not isinstance(arg, exp.Star):
                ts += [Target("NULLCOL", q, r) for r in _nullable_refs(qi, arg, schema)]
            if dist or isinstance(agg, (exp.Sum, exp.Avg, exp.Count)):
                ts += [Target("DUP", q, (o,)) for o in _grain(qi, sc, schema)]
            add(("agg", type(agg).__name__.upper(), dist, argsql), *ts)
        h = s.args.get("having")
        if h is not None:
            for a in atoms_of(h.this):
                if a.find(exp.Count) is None:
                    continue
                for lit in a.find_all(exp.Literal):
                    if lit.is_string:
                        continue
                    try:
                        c = int(float(lit.this))
                    except ValueError:
                        continue
                    ts = [Target("GROUPSIZE", q, (o, n)) for o in _grain(qi, sc, schema)
                          for n in (c - 1, c, c + 1) if 1 <= n <= 12]
                    add(("having-count", qi.norm_sql(a)), *ts)
        for w in s.find_all(exp.Window):
            if id(_owner(w)) == sid:
                add(("window", qi.norm_sql(w)), *[Target("TIE", q, (o, 2)) for o in sc.occs], *dup_scope)
        if sc.role == "main" or isinstance(s.parent, (exp.Union, exp.Except, exp.Intersect)):
            add(("proj", sc.role, tuple(qi.norm_sql(e) for e in s.expressions)))
    # ORDER BY / LIMIT / set operations at the top
    top = qi.tree
    order, limit = top.args.get("order"), top.args.get("limit")
    n = None
    if limit is not None:
        try:
            n = int(limit.expression.this) if limit.expression is not None else int(limit.this.this)
        except Exception:
            n = 1
    if order is not None:
        keys = tuple(qi.norm_sql(o) for o in order.expressions)
        ts = []
        for o in order.expressions:
            for c in o.find_all(exp.Column):
                r = qi.resolve(c)
                if r is not None:
                    ts.append(Target("TIE", q, (r[0], min(n or 1, 5))))
        if not ts:
            main = top if isinstance(top, exp.Select) else None
            if main is not None and id(main) in qi.scopes:
                ts = [Target("TIE", q, (o, min(n or 1, 5))) for o in _grain(qi, qi.scopes[id(main)], schema)]
        add(("order", keys), *ts)
    if n is not None:
        lim_ts = list(F.get(("order", tuple(qi.norm_sql(o) for o in order.expressions)), [])) if order is not None else []
        add(("limit", n), *lim_ts)
    if isinstance(top, (exp.Union, exp.Except, exp.Intersect)):
        add(("setop", type(top).__name__, bool(top.args.get("distinct"))),
            *[Target("DUP", q, (o.oid,)) for o in qi.occ])
    return F


def _owner(node):
    p = node.parent
    while p is not None and not isinstance(p, exp.Select):
        p = p.parent
    return p


def fallback(qi: QInfo, q: int, schema: Schema):
    """Tier-1 targets of one query (APC-like coverage plus shapes)."""
    out = []
    if not qi.ok:
        return out
    seen = set()
    for f in qi.formulas:
        for a in atoms_of(f.node):
            key = qi.norm_sql(a)
            for p in ("T", "F", "N"):
                t = Target("PRED", q, (key, p), 1)
                if t.key not in seen:
                    seen.add(t.key); out.append(t)
    for o in qi.occ:
        out.append(Target("DUP", q, (o.oid,), 1))
    cols = set()
    for c in qi.tree.find_all(exp.Column):
        r = qi.column_table(c)
        if r is not None and schema.column(*r).nullable:
            cols.add(r)
    out += [Target("NULLCOL", q, r, 1) for r in sorted(cols)]
    for sid, sc in qi.scopes.items():
        if sc.role in ("pred", "scalar") and _scope_oids(qi, sid):
            out.append(Target("EMPTY", q, _scope_oids(qi, sid), 1))
    out += [Target("AGG", q, (k, m), 1) for k, m in agg_atoms(qi)]
    return out


def unary_atoms(qi: QInfo, schema: Schema):
    """{atom key: (template over x0, (table, col))} for single-column atoms on a non-key column in scopes whose
    formulas are asserted on the witness row (main / derived)."""
    from .generate import template
    out = {}
    if not qi.ok:
        return out
    for f in qi.formulas:
        if qi.scopes[f.sid].role not in ("main", "derived") or f.kind not in ("where", "on", "having"):
            continue
        for a in atoms_of(f.node):
            refs = qi.refs(a)
            if not refs or len(refs) != 1 or a.find(exp.AggFunc) is not None:
                continue
            tc = (qi.occ_by_id(refs[0][0]).table, refs[0][1])
            if schema.is_key(*tc):
                continue
            t = template(a, qi, refs)
            if t is not None:
                out[qi.norm_sql(a)] = (t, tc)
    return out


def pair_targets(fa, fb, qa, qb, ua=None, ub=None):
    """Targets from the symmetric difference of two members' features. PREDPAIR: an atom of one member and a
    different atom of the other member on the same column must disagree on the same row (e.g. salary > 10 vs
    salary >= 10 needs salary = 10); realised on the first member with the other's atom as an extra constraint."""
    out = []
    for feat in set(fa) ^ set(fb):
        out += fa.get(feat, []) if feat in fa else fb.get(feat, [])
    if ua and ub:
        for ka, (ta, ca) in ua.items():
            if ka in ub:
                continue
            for kb, (tb, cb) in ub.items():
                if kb in ua or cb != ca:
                    continue
                for pa, pb in (("T", "F"), ("F", "T"), ("T", "N"), ("N", "T")):
                    out.append(Target("PREDPAIR", qa, (ka, pa, tb, cb, pb)))
                    out.append(Target("PREDPAIR", qb, (kb, pb, ta, ca, pa)))
    # a feature present in both with no textual difference gives nothing; if the difference is only in features
    # that carry no targets (e.g. projection), the caller falls back to tier 1.
    return out
