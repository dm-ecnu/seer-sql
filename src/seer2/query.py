"""Query analysis shared by diff.py and generate.py: scopes, table occurrences, formulas, atoms.

Every SELECT node is a scope. Its role decides how its formulas enter the witness encoding:
  main / derived (FROM subquery, CTE, set-op branch): WHERE / ON / HAVING are asserted (default: TRUE);
  pred (EXISTS / IN subquery): its WHERE only enters through the EXISTS / IN atom of the enclosing scope;
  scalar (scalar subquery, ANY/ALL): asserted (default TRUE: a non-empty subquery), the atom using it is free.
Columns are resolved to (occurrence, base column), through derived tables / CTEs whose projection is a plain column,
and to enclosing scopes (correlation).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .schema import Schema

CMP = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.Like, exp.ILike, exp.Is,
       exp.Exists, exp.NullSafeEQ, exp.NullSafeNEQ)
BOOLOPS = (exp.And, exp.Or, exp.Not, exp.Paren)


@dataclass
class Occ:
    oid: str
    table: str
    alias: str
    sid: int


@dataclass
class Scope:
    sid: int
    node: exp.Select
    parent: int | None
    role: str
    pred: exp.Expression | None = None
    sources: dict = field(default_factory=dict)     # alias -> ("base", oid) | ("derived", select node)
    occs: list = field(default_factory=list)


@dataclass
class Formula:
    fid: int
    sid: int
    kind: str           # where | on | having | case
    node: exp.Expression
    side: str = ""      # join side/kind for ON formulas


def _scope_node(node):
    p = node.parent
    while p is not None and not isinstance(p, exp.Select):
        p = p.parent
    return p


class QInfo:
    def __init__(self, sql: str, schema: Schema, dialect="duckdb"):
        self.sql, self.schema, self.dialect = sql, schema, dialect
        self.ok = True
        self.err = None
        try:
            self.tree = sqlglot.parse_one(sql, read=dialect)
        except Exception as e:
            self.ok, self.err = False, f"parse: {e}"
            self.tree = None
            return
        self.ctes = {}
        for cte in self.tree.find_all(exp.CTE):
            self.ctes[cte.alias_or_name.lower()] = cte.this
        self.scopes: dict[int, Scope] = {}
        self.occ: list[Occ] = []
        sels = list(self.tree.find_all(exp.Select))
        for s in sels:
            par = _scope_node(s)
            role, pred = self._role(s)
            self.scopes[id(s)] = Scope(id(s), s, id(par) if par is not None else None, role, pred)
        for s in sels:
            self._sources(self.scopes[id(s)])
        self.formulas: list[Formula] = []
        for s in sels:
            sc = self.scopes[id(s)]
            w = s.args.get("where")
            if w is not None:
                self.formulas.append(Formula(len(self.formulas), sc.sid, "where", w.this))
            for j in s.args.get("joins") or []:
                on = j.args.get("on")
                if on is not None:
                    side = (j.args.get("side") or j.args.get("kind") or "INNER")
                    self.formulas.append(Formula(len(self.formulas), sc.sid, "on", on, str(side).upper()))
            h = s.args.get("having")
            if h is not None:
                self.formulas.append(Formula(len(self.formulas), sc.sid, "having", h.this))
            for e in s.expressions:
                for c in e.find_all(exp.Case):
                    if _scope_node(c) is not s:
                        continue
                    for ifs in c.args.get("ifs") or []:
                        self.formulas.append(Formula(len(self.formulas), sc.sid, "case", ifs.this))
                for c in e.find_all(exp.If):
                    if _scope_node(c) is s:
                        self.formulas.append(Formula(len(self.formulas), sc.sid, "case", c.this))
        self.atoms = []          # (formula id, atom node)
        for f in self.formulas:
            for a in atoms_of(f.node):
                self.atoms.append((f.fid, a))

    # ------------------------------------------------------------------ scopes
    def _role(self, s):
        p, child = s.parent, s
        while p is not None and not isinstance(p, exp.Select):
            if isinstance(p, exp.CTE):
                return "derived", None
            if isinstance(p, exp.Exists):
                return "pred", p
            if isinstance(p, exp.Subquery):
                g = p.parent
                if isinstance(g, (exp.From, exp.Join)):
                    return "derived", None
                if isinstance(g, exp.In) and g.args.get("query") is p:
                    return "pred", g
                return "scalar", None
            child, p = p, p.parent
        return ("main", None) if p is None else ("derived", None)

    def _sources(self, sc):
        s = sc.node
        srcs = []
        fr = s.args.get("from") or s.args.get("from_")
        if fr is not None:
            srcs.append(fr.this)
        for j in s.args.get("joins") or []:
            srcs.append(j.this)
        for src in srcs:
            if isinstance(src, exp.Table):
                name = src.name.lower()
                alias = (src.alias or src.name).lower()
                if name in self.ctes and not src.args.get("db"):
                    sc.sources[alias] = ("derived", self.ctes[name])
                elif name in self.schema.tables:
                    oid = f"o{len(self.occ)}"
                    o = Occ(oid, name, alias, sc.sid)
                    self.occ.append(o)
                    sc.occs.append(oid)
                    sc.sources[alias] = ("base", oid)
            elif isinstance(src, exp.Subquery):
                alias = (src.alias or "").lower()
                sc.sources[alias] = ("derived", src.this)

    def occ_by_id(self, oid):
        return self.occ[int(oid[1:])]

    # ------------------------------------------------------------------ resolution
    def resolve(self, col: exp.Column, sid=None, depth=0):
        """Column -> (oid, base column) or None."""
        if depth > 8:
            return None
        if sid is None:
            sn = _scope_node(col)
            sid = id(sn) if sn is not None else None
        name = col.name.lower()
        q = col.table.lower() if col.table else None
        while sid is not None:
            sc = self.scopes.get(sid)
            if sc is None:
                return None
            if q is not None:
                if q in sc.sources:
                    return self._from_source(sc.sources[q], name, depth)
            else:
                for alias, src in sc.sources.items():
                    r = self._from_source(src, name, depth, strict=True)
                    if r is not None:
                        return r
            sid = sc.parent
        return None

    def _from_source(self, src, name, depth, strict=False):
        kind, x = src
        if kind == "base":
            t = self.occ_by_id(x).table
            return (x, name) if name in self.schema.tables[t].colnames else None
        sel = x
        while isinstance(sel, (exp.Union, exp.Except, exp.Intersect)):
            sel = sel.this
        if not isinstance(sel, exp.Select) or id(sel) not in self.scopes:
            return None
        for e in sel.expressions:
            if isinstance(e, exp.Star):
                return self.resolve(exp.column(name), id(sel), depth + 1)
            if e.alias_or_name.lower() == name:
                inner = e.this if isinstance(e, exp.Alias) else e
                if isinstance(inner, exp.Column):
                    return self.resolve(inner, id(sel), depth + 1)
                return None
        return None

    def refs(self, node):
        """Distinct resolved base references of a node; None if some column cannot be resolved."""
        if node.find(exp.Select) is not None:
            return None
        out = []
        for c in node.find_all(exp.Column):
            r = self.resolve(c)
            if r is None:
                return None
            if r not in out:
                out.append(r)
        return out

    def norm_sql(self, node):
        """SQL of a node with columns replaced by base table.column (aliases removed); used as atom identity."""
        e = node.copy()
        orig = list(node.find_all(exp.Column))
        copies = list(e.find_all(exp.Column))
        for o, c in zip(orig, copies):
            r = self.resolve(o)
            if r is not None:
                c.replace(exp.column(r[1], table=self.occ_by_id(r[0]).table))
            else:
                c.replace(exp.column(c.name.lower(), table="?"))
        for t in e.find_all(exp.Table):
            t.set("alias", None)
        return canon(e).sql(dialect="duckdb")

    def column_table(self, col):
        r = self.resolve(col)
        return None if r is None else (self.occ_by_id(r[0]).table, r[1])


_FLIP = {exp.GT: exp.LT, exp.GTE: exp.LTE}
_SYM = (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.NullSafeNEQ)


def canon(e):
    """Commutation-insensitive form of an alias-free expression, so that rewrites differing only in operand order
    (a = b vs b = a, a > b vs b < a, AND / OR operand order) give the same atom / feature identity."""
    def tr(n):
        if isinstance(n, _SYM):
            a, b = n.this, n.expression
            if a.sql() > b.sql():
                return type(n)(this=b, expression=a)
        elif type(n) in _FLIP:
            return _FLIP[type(n)](this=n.expression, expression=n.this)
        elif isinstance(n, (exp.And, exp.Or)):
            a, b = n.this, n.expression
            if a.sql() > b.sql():
                return type(n)(this=b, expression=a)
        return n
    return e.transform(tr)


def atoms_of(node):
    """Atoms of a boolean formula (maximal non-connective subterms)."""
    if isinstance(node, BOOLOPS):
        out = []
        for k in ("this", "expression"):
            ch = node.args.get(k)
            if ch is not None:
                out += atoms_of(ch)
        return out
    return [node]
