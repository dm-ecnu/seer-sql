"""sqlglot front end: SQL text (DuckDB / PostgreSQL / SQLite / MySQL) -> SEER IR (seer.ir).

Replaces Polygon's mo-sql-parsing front end (vendor/polygon/sql_testing/sql_parser.py), which rejected 105 of 203
TPC-H rewrite pairs in our run. Steps: parse with the source dialect, qualify every column against the schema
(sqlglot.optimizer.qualify: expands *, resolves aliases, ordinals and GROUP BY alias references), then translate
one query block at a time:

  FROM/JOIN -> WHERE -> GROUP BY/aggregates -> HAVING -> window functions -> SELECT -> DISTINCT -> ORDER BY/LIMIT

Column references become positional (de Bruijn) references, see ir.py. Unsupported constructs raise
ir.Unsupported(code); nothing is dropped silently.
"""
from __future__ import annotations

import datetime as dt
import decimal

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.optimizer.qualify import qualify

from . import ir
from .funcs import STR_MAPS, STR_NUMS, add_months
from .ir import BOOL, DATE, FLOAT, INT, NULL, STR, TS, E, R, Unsupported, fn, is_dec, is_num, lit

_TYPE_OF = {"int": INT, "float": FLOAT, "str": STR, "date": DATE, "ts": TS, "bool": BOOL}


def schema_types(schema):
    """seer2 Schema -> {table: [(col, ir type)]}."""
    out = {}
    for t in schema.order:
        cols = []
        for c in schema.tables[t].columns:
            if c.type == "dec":
                ty = ir.DEC((c.param or (18, 2))[1])
            else:
                ty = _TYPE_OF[c.type]
            cols.append((c.name, ty))
        out[t] = cols
    return out


def null_order(dialect):
    """nulls_first flag for (asc, desc) under the dialect's default NULL ordering."""
    no = Dialect.get_or_raise(dialect).NULL_ORDERING
    if no == "nulls_are_small":
        return True, False
    if no == "nulls_are_large":
        return False, True
    return False, False          # nulls_are_last


# ---------------------------------------------------------------------- scopes
class FromScope:
    """Columns of a block's FROM row: [(alias, name, type)]."""

    def __init__(self, cols):
        self.cols = list(cols)

    def lookup(self, table, name):
        hits = [i for i, (a, n, _) in enumerate(self.cols) if n == name and (table is None or a == table)]
        if not hits:
            return None
        if len(hits) > 1 and table is None:
            raise Unsupported("resolve", f"ambiguous column {name}")
        i = hits[0]
        return ir.col(i, self.cols[i][2])


class AggScope:
    """Post-aggregation row: keys ++ aggregates. A FROM column that is a grouping key resolves to the key; any
    other FROM column becomes ANY_VALUE(column) (exact when it is functionally dependent on the keys, which is the
    only case PostgreSQL/DuckDB accept; SQLite/MySQL pick an arbitrary row, so the engine decides)."""

    def __init__(self, inner: FromScope, keys, aggs):
        self.inner, self.keys, self.aggs = inner, keys, aggs

    def lookup(self, table, name):
        e = self.inner.lookup(table, name)
        if e is None:
            return None
        for i, k in enumerate(self.keys):
            if k == e:
                return ir.col(i, e.ty)
        return self.agg_ref(ir.Agg("any_value", False, (e,), e.ty))

    def agg_ref(self, a):
        if a not in self.aggs:
            self.aggs.append(a)
        return ir.col(len(self.keys) + self.aggs.index(a), a.ty)


class RowScope:
    """A row whose columns are named (derived row of a set operation or a projected relation)."""

    def __init__(self, names, types):
        self.names, self.types = list(names), list(types)

    def lookup(self, table, name):
        if name in self.names:
            i = self.names.index(name)
            return ir.col(i, self.types[i])
        return None


# ---------------------------------------------------------------------- the translator
class Front:
    def __init__(self, schema, dialect="duckdb"):
        self.schema = schema
        self.dialect = dialect
        self.tables = schema_types(schema)
        self.sg_schema = {t: {c: self._sgtype(t, c) for c, _ in cols} for t, cols in self.tables.items()}
        self.nulls_first_asc, self.nulls_first_desc = null_order(dialect)

    def _sgtype(self, t, c):
        return self.schema.column(t, c).raw

    # .................................................................. entry
    def translate(self, sql) -> R:
        try:
            trees = sqlglot.parse(sql, read=self.dialect)
        except Exception as e:
            raise Unsupported("parse", str(e)[:200])
        trees = [t for t in trees if t is not None]
        if len(trees) != 1:
            raise Unsupported("parse", f"{len(trees)} statements")
        tree = trees[0]
        if not isinstance(tree, exp.Query):
            raise Unsupported("statement", type(tree).__name__)
        tree = self._normalize_names(tree)
        try:
            tree = qualify(tree, schema=self.sg_schema, dialect=self.dialect, quote_identifiers=False,
                           validate_qualify_columns=True, identify=False)
        except Unsupported:
            raise
        except Exception as e:
            raise Unsupported("resolve", str(e)[:200])
        self.ctes = [{}]
        return self.query(tree, [], top=True)

    def _normalize_names(self, tree):
        """Engine identifier rules sqlglot's qualify does not apply for these dialects: MySQL, SQLite and DuckDB
        match column/table names case-insensitively (quoted or not); SQLite reads a double-quoted token that names
        no column or alias as a string literal."""
        if self.dialect not in ("mysql", "sqlite", "duckdb"):
            return tree
        if self.dialect == "sqlite":
            known = {c.lower() for cols in self.sg_schema.values() for c in cols}
            known |= {a.alias.lower() for a in tree.find_all(exp.Alias)}
            known |= {t.alias.lower() for t in tree.find_all(exp.TableAlias) if t.alias}
            for c in list(tree.find_all(exp.Column)):
                ident = c.this
                if (isinstance(ident, exp.Identifier) and ident.args.get("quoted") and not c.table
                        and ident.this.lower() not in known):
                    c.replace(exp.Literal.string(ident.this))
        for i in tree.find_all(exp.Identifier):
            i.set("this", i.this.lower())
        return tree

    # .................................................................. query = block or set operation
    def query(self, q, stack, top=False):
        if isinstance(q, exp.Subquery):
            inner = self.query(q.this, stack, top)
            return self._order_limit(q, inner, stack, top) if (q.args.get("order") or q.args.get("limit")) else inner
        if isinstance(q, exp.Paren):
            return self.query(q.this, stack, top)
        with_ = q.args.get("with") or q.args.get("with_")
        pushed = False
        if with_ is not None:
            if with_.args.get("recursive"):
                raise Unsupported("recursive_cte")
            # Keep each lexical WITH scope separate: a nonrecursive CTE body
            # cannot see its own declaration, but may see an outer one.
            self.ctes.append({})
            pushed = True
            for cte in with_.expressions:
                name = cte.alias_or_name.lower()
                self.ctes[-1][name] = (cte.this, cte.args.get("alias"))
        try:
            if isinstance(q, exp.Select):
                return self.block(q, stack, top)
            if isinstance(q, (exp.Union, exp.Intersect, exp.Except)):
                return self.setop(q, stack, top)
            if isinstance(q, exp.Values):
                return self.values(q)
            raise Unsupported("query_kind", type(q).__name__)
        finally:
            if pushed:
                self.ctes.pop()

    def setop(self, q, stack, top):
        kind = {exp.Union: "union", exp.Intersect: "intersect", exp.Except: "except"}[type(q)]
        distinct = q.args.get("distinct")
        all_ = distinct is False
        if q.args.get("by_name"):
            raise Unsupported("union_by_name")
        left = self.query(q.this, stack)
        right = self.query(q.expression, stack)
        if left.arity != right.arity:
            raise Unsupported("resolve", "set operation arity mismatch")
        types = [_unify(a, b) for a, b in zip(left.types, right.types)]
        r = R("setop", (kind, all_, left, right), types, left.names)
        if q.args.get("order") or q.args.get("limit") or q.args.get("offset"):
            r = self._order_limit(q, r, stack, top)
        return r

    def values(self, v):
        rows = []
        for t in v.expressions:
            items = t.expressions if isinstance(t, exp.Tuple) else [t]
            rows.append(tuple(self.expr(x, Ctx([])) for x in items))
        if not rows:
            raise Unsupported("values_empty")
        types = [_unify_all([r[i].ty for r in rows]) for i in range(len(rows[0]))]
        alias = v.args.get("alias")
        names = [c.name.lower() for c in alias.columns] if alias is not None and alias.columns else None
        return R("values", (tuple(rows),), types, names)

    def _order_limit(self, q, rel, stack, top):
        """ORDER BY / LIMIT over a finished relation whose output columns are named rel.names."""
        order = q.args.get("order")
        keys = []
        if order is not None:
            sc = RowScope(rel.names, rel.types)
            for o in order.expressions:
                e = o.this
                if isinstance(e, exp.Literal) and e.is_int:
                    i = int(e.this) - 1
                    ke = ir.col(i, rel.types[i])
                else:
                    ke = self.expr(e, Ctx([*stack, sc]))
                keys.append(self._ordered(o, ke))
        lim, off = self._limit(q)
        if not keys and lim is None and off is None:
            return rel
        if not top and lim is None and off is None:
            return rel           # ORDER BY without LIMIT in a subquery does not affect the bag
        return R("sort", (rel, tuple(keys), lim, off), rel.types, rel.names)

    def _ordered(self, o, ke):
        desc = bool(o.args.get("desc"))
        nf = o.args.get("nulls_first")
        if nf is None:
            nf = self.nulls_first_desc if desc else self.nulls_first_asc
        return (ke, desc, bool(nf))

    def _limit(self, q):
        lim = off = None
        L = q.args.get("limit")
        if L is not None:
            x = L.expression if isinstance(L, exp.Limit) else L
            if isinstance(L, exp.Fetch):
                x = L.args.get("count")
            lim = self._const_int(x, "limit")
        O = q.args.get("offset")
        if O is not None:
            off = self._const_int(O.expression, "offset")
        return lim, off

    def _const_int(self, x, what):
        if isinstance(x, exp.Literal) and x.is_int:
            return int(x.this)
        raise Unsupported(f"{what}_expr", x.sql() if x is not None else "")

    # .................................................................. FROM
    def source(self, src, stack):
        """-> (R, [(alias, name, type)])"""
        if isinstance(src, exp.Table):
            if src.args.get("joins"):
                raise Unsupported("nested_join_syntax")
            name = src.name.lower()
            alias = (src.alias_or_name or name).lower()
            if src.args.get("db") or src.args.get("catalog"):
                raise Unsupported("qualified_table", src.sql())
            for scope in reversed(self.ctes):
                if name in scope:
                    cq, calias = scope[name]
                    # A nonrecursive CTE definition sees only declarations
                    # preceding itself. Its own name may still denote a base
                    # table (or an outer CTE) inside the definition.
                    saved = list(scope.items())
                    preceding = dict(saved[:list(scope).index(name)])
                    scope.clear()
                    scope.update(preceding)
                    try:
                        r = self.query(cq, [])
                    finally:
                        scope.clear()
                        scope.update(saved)
                    if ir.free_depth(r) > 0:
                        raise Unsupported("correlated_cte")
                    names = list(r.names)
                    if calias is not None and calias.columns:
                        names = [c.name.lower() for c in calias.columns]
                    ta = src.args.get("alias")
                    if ta is not None and ta.columns:
                        names = [c.name.lower() for c in ta.columns]
                    return r, [(alias, n, t) for n, t in zip(names, r.types)]
            if name not in self.tables:
                raise Unsupported("resolve", f"unknown table {name}")
            cols = self.tables[name]
            ta = src.args.get("alias")
            names = [c for c, _ in cols]
            if ta is not None and ta.columns:
                names = [c.name.lower() for c in ta.columns]
            r = R("scan", (name,), [t for _, t in cols], [c for c, _ in cols])
            return r, [(alias, n, t) for n, (_, t) in zip(names, cols)]
        if isinstance(src, exp.Subquery):
            if src.args.get("lateral"):
                raise Unsupported("lateral")
            r = self.query(src.this, stack)
            if src.args.get("order") or src.args.get("limit"):
                r = self._order_limit(src, r, stack, False)
            alias = (src.alias or "").lower()
            names = list(r.names)
            ta = src.args.get("alias")
            if ta is not None and ta.columns:
                names = [c.name.lower() for c in ta.columns]
            return r, [(alias, n, t) for n, t in zip(names, r.types)]
        if isinstance(src, exp.Values):
            r = self.values(src)
            alias = (src.alias or "").lower()
            return r, [(alias, n, t) for n, t in zip(r.names, r.types)]
        if isinstance(src, exp.Lateral):
            raise Unsupported("lateral")
        if isinstance(src, exp.Unnest):
            raise Unsupported("unnest")
        raise Unsupported("from_kind", type(src).__name__)

    def from_clause(self, sel, stack):
        frm = sel.args.get("from") or sel.args.get("from_")
        if frm is None:
            return R("values", (((),),), []), []
        rel, cols = self.source(frm.this, stack)
        for j in sel.args.get("joins") or []:
            rel, cols = self.join(rel, cols, j, stack)
        return rel, cols

    def join(self, left, lcols, j, stack):
        lateral = isinstance(j.this, exp.Lateral) or (isinstance(j.this, exp.Subquery) and j.this.args.get("lateral"))
        if lateral:
            # the right side is evaluated once per left row: it sees the left columns as outer level 1
            node = j.this
            sub = node.this if isinstance(node, exp.Lateral) else node
            if not isinstance(sub, exp.Subquery):
                raise Unsupported("lateral", f"lateral {type(sub).__name__}")
            lstack = [*stack, FromScope(lcols)]
            right = self.query(sub.this, lstack)
            if sub.args.get("order") or sub.args.get("limit"):
                right = self._order_limit(sub, right, lstack, False)
            alias = (node.alias or sub.alias or "").lower()
            names = list(right.names)
            ta = node.args.get("alias") or sub.args.get("alias")
            if ta is not None and ta.columns:
                names = [c.name.lower() for c in ta.columns]
            rcols = [(alias, n, t) for n, t in zip(names, right.types)]
            lateral = ir.free_depth(right) > 0
        else:
            right, rcols = self.source(j.this, stack)
        side = (j.side or "").lower()
        kind = (j.kind or "").lower()
        if j.args.get("method"):
            raise Unsupported("join_method", j.args["method"])
        if kind in ("semi", "anti"):
            jk = kind
        elif kind == "cross":
            jk = "cross"
        elif side in ("left", "right", "full"):
            jk = side
        else:
            jk = "inner"
        cols = lcols + rcols
        sc = FromScope(cols)
        on = j.args.get("on")
        using = j.args.get("using")
        if using:
            conds = []
            for u in using:
                n = u.name.lower()
                li = next((i for i, c in enumerate(lcols) if c[1] == n), None)
                ri = next((i for i, c in enumerate(rcols) if c[1] == n), None)
                if li is None or ri is None:
                    raise Unsupported("resolve", f"USING column {n}")
                conds.append(fn("eq", [ir.col(li, lcols[li][2]), ir.col(len(lcols) + ri, rcols[ri][2])], BOOL))
            cond = _and(conds)
        elif on is not None:
            cond = self.bool_expr(on, Ctx([*stack, sc]))
        else:
            if jk in ("left", "right", "full", "semi", "anti"):
                raise Unsupported("join_without_condition", jk)
            jk = "cross"
            cond = ir.TRUE
        if jk == "cross" and cond != ir.TRUE:
            jk = "inner"
        types = left.types + right.types
        names = left.names + right.names
        if lateral:
            if jk not in ("inner", "cross", "left"):
                raise Unsupported("lateral", f"{jk} lateral join")
            jk = "lat_" + jk
        if jk in ("semi", "anti"):
            return R("join", (jk, left, right, cond), left.types, left.names), lcols
        return R("join", (jk, left, right, cond), types, names), cols

    # .................................................................. one SELECT block
    def block(self, sel: exp.Select, stack, top):
        if sel.args.get("into"):
            raise Unsupported("select_into")
        for k in ("qualify", "connect", "prewhere", "laterals", "match", "sample", "settings", "pivots"):
            if sel.args.get(k):
                raise Unsupported(k)
        rel, cols = self.from_clause(sel, stack)
        fscope = FromScope(cols)
        where = sel.args.get("where")
        if where is not None:
            rel = R("filter", (rel, self.bool_expr(where.this, Ctx([*stack, fscope]))), rel.types, rel.names)

        projs = list(sel.expressions)
        order = sel.args.get("order")
        having = sel.args.get("having")
        group = sel.args.get("group")
        if group is not None and (any(group.args.get(k) for k in ("grouping_sets", "cube", "rollup")) or
                                  any(isinstance(g, (exp.Rollup, exp.Cube, exp.GroupingSets))
                                      for g in group.expressions)):
            return self.query(_expand_grouping_sets(sel), stack, top)
        if any(isinstance(g, exp.Grouping) for x in projs for g in x.find_all(exp.Grouping)):
            raise Unsupported("grouping_without_sets")
        agg_nodes = []
        for x in projs + ([having.this] if having is not None else []) + \
                ([o.this for o in order.expressions] if order is not None else []):
            agg_nodes += _aggs_in(x)
        scope = fscope
        subst = {}
        aggs = None
        if group is not None or agg_nodes:
            kexprs = []
            for g in (group.expressions if group is not None else []):
                if isinstance(g, exp.Literal) and g.is_int:
                    g = projs[int(g.this) - 1]
                    g = g.this if isinstance(g, exp.Alias) else g
                kexprs.append(g)
            keys = [self.expr(g, Ctx([*stack, fscope])) for g in kexprs]
            aggs = []
            ascope = AggScope(fscope, keys, aggs)
            for g, ke in zip(kexprs, keys):
                subst[_k(g)] = ir.col(keys.index(ke), ke.ty)
            for a in agg_nodes:
                ag = self.agg(a, Ctx([*stack, fscope]))
                subst[_k(a)] = ascope.agg_ref(ag)
            scope = ascope
        # window functions
        wins = []
        for x in projs + ([o.this for o in order.expressions] if order is not None else []):
            wins += [w for w in x.find_all(exp.Window) if not _inside_subquery(w, x)]
        base_types = None
        if aggs is not None:
            pre = None
        win_specs = []
        for w in wins:
            if _k(w) in subst:
                continue
            spec = self.window(w, Ctx([*stack, scope], subst))
            win_specs.append((w, spec))
        # having (before windows: evaluated on the aggregate row)
        hav = None
        if having is not None:
            hav = self.bool_expr(having.this, Ctx([*stack, scope], subst))
        # select list
        out_exprs, out_names = [], []
        if win_specs:
            # window columns are appended to the (post-aggregation) row
            nbase = None
        sel_ctx = Ctx([*stack, scope], subst)
        win_index = {}
        for i, (w, spec) in enumerate(win_specs):
            win_index[_k(w)] = (i, spec)
        sel_ctx.windows = win_index
        for p in projs:
            if isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)):
                raise Unsupported("resolve", "unexpanded *")
            name = p.alias_or_name.lower() if p.alias_or_name else f"c{len(out_names)}"
            e = p.this if isinstance(p, exp.Alias) else p
            out_exprs.append(self.expr(e, sel_ctx))
            out_names.append(name)
        # ORDER BY keys: an output alias / ordinal, or an expression over the pre-projection row (hidden column)
        okeys = []
        hidden = []
        if order is not None:
            for o in order.expressions:
                e = o.this
                idx = None
                if isinstance(e, exp.Literal) and e.is_int:
                    idx = int(e.this) - 1
                elif isinstance(e, exp.Column) and not e.table and e.name.lower() in out_names:
                    idx = out_names.index(e.name.lower())
                else:
                    ke = self.expr(e, sel_ctx)
                    if ke in out_exprs:
                        idx = out_exprs.index(ke)
                    else:
                        hidden.append(ke)
                        idx = len(out_exprs) + len(hidden) - 1
                okeys.append((o, idx))
        lim, off = self._limit(sel)
        sorted_ = bool(okeys) and (top or lim is not None or off is not None)
        distinct = sel.args.get("distinct")
        don = None
        if distinct is not None and distinct.args.get("on"):
            # DISTINCT ON (e, ...): first row of each e-group under ORDER BY (which then matters even when this
            # block is not the top one); its keys are output columns or hidden columns like ORDER BY keys.
            don = []
            for e in distinct.args["on"].expressions:
                if isinstance(e, exp.Literal) and e.is_int:
                    don.append(int(e.this) - 1)
                    continue
                if isinstance(e, exp.Column) and not e.table and e.name.lower() in out_names:
                    don.append(out_names.index(e.name.lower()))
                    continue
                ke = self.expr(e, sel_ctx)
                if ke in out_exprs:
                    don.append(out_exprs.index(ke))
                elif ke in hidden:
                    don.append(len(out_exprs) + hidden.index(ke))
                else:
                    hidden.append(ke)
                    don.append(len(out_exprs) + len(hidden) - 1)
        elif not sorted_:
            hidden = []
        # assemble: agg -> having -> window -> project -> distinct -> sort -> drop hidden
        if aggs is not None:
            ktypes = [k.ty for k in keys]
            rel = R("agg", (rel, tuple(keys), tuple(aggs)), ktypes + [a.ty for a in aggs])
            if hav is not None:
                rel = R("filter", (rel, hav), rel.types)
        elif hav is not None:
            raise Unsupported("resolve", "HAVING without aggregation")   # unreachable: HAVING makes it an aggregate
        if win_specs:
            nb = rel.arity
            rel = R("window", (rel, tuple(s for _, s in win_specs)), list(rel.types) + [s.ty for _, s in win_specs])
            out_exprs = [_shift_win(e, nb) for e in out_exprs]
            hidden = [_shift_win(e, nb) for e in hidden]
        all_out = out_exprs + hidden
        rel = R("project", (rel, tuple(all_out)), [e.ty for e in all_out],
                out_names + [f"_h{i}" for i in range(len(hidden))])
        if don is not None:
            okx = tuple(self._ordered(o, ir.col(i, rel.types[i])) for o, i in okeys)
            rel = R("distinct_on", (rel, tuple(don), okx), rel.types, rel.names)
        elif distinct is not None:
            if hidden:
                raise Unsupported("order_by_hidden_with_distinct")
            rel = R("distinct", (rel,), rel.types, rel.names)
        if sorted_:
            keys = tuple(self._ordered(o, ir.col(i, rel.types[i])) for o, i in okeys)
            rel = R("sort", (rel, keys, lim, off), rel.types, rel.names)
            if hidden:
                n = len(out_exprs)
                rel = R("project", (rel, tuple(ir.col(i, rel.types[i]) for i in range(n))), rel.types[:n],
                        out_names)
        elif lim is not None or off is not None:
            rel = R("sort", (rel, (), lim, off), rel.types, rel.names)
        if hidden and not sorted_:
            n = len(out_exprs)
            rel = R("project", (rel, tuple(ir.col(i, rel.types[i]) for i in range(n))), rel.types[:n], out_names)
        return rel

    # .................................................................. aggregates and windows
    def agg(self, a, ctx):
        filt = None
        if isinstance(a, exp.Filter):
            filt = self.bool_expr(a.expression.this, ctx)
            a = a.this
        distinct = False
        arg = a.this
        if isinstance(arg, exp.Distinct):
            distinct = True
            if len(arg.expressions) != 1:
                raise Unsupported("agg_multi_distinct")
            arg = arg.expressions[0]
        name = _AGG_NAMES.get(type(a))
        if name is None:
            raise Unsupported("agg_func", type(a).__name__)
        if name == "count" and (arg is None or isinstance(arg, exp.Star)):
            return ir.Agg("count_star", False, (), INT, filt)
        if a.args.get("expressions") and name != "count":
            raise Unsupported("agg_args", a.sql())
        e = self.expr(arg, ctx)
        if name == "count":
            ty = INT
        elif name == "sum":
            ty = e.ty if (e.ty == INT or is_dec(e.ty)) else FLOAT
            if e.ty == BOOL:
                raise Unsupported("agg_type", "sum(bool)")
        elif name == "avg":
            if not is_num(e.ty):
                raise Unsupported("agg_type", f"avg({e.ty})")
            ty = FLOAT
        else:
            ty = e.ty
        return ir.Agg(name, distinct, (e,), ty, filt)

    def window(self, w: exp.Window, ctx):
        f = w.this
        part = [self.expr(p, ctx) for p in (w.args.get("partition_by") or [])]
        order = []
        if w.args.get("order") is not None:
            for o in w.args["order"].expressions:
                order.append(self._ordered(o, self.expr(o.this, ctx)))
        spec = w.args.get("spec")
        frame = "default"
        if spec is not None:
            s = (spec.args.get("start") or "").upper() if isinstance(spec.args.get("start"), str) else spec.args.get("start")
            e = spec.args.get("end")
            kind = (spec.args.get("kind") or "").upper()
            st = spec.args.get("start")
            st_s = st if isinstance(st, str) else (st.sql() if st is not None else "")
            en_s = e if isinstance(e, str) else (e.sql() if e is not None else "")
            st_side = (spec.args.get("start_side") or "").upper()
            en_side = (spec.args.get("end_side") or "").upper()
            if st_s.upper() == "UNBOUNDED" and st_side == "PRECEDING" and en_s.upper() == "UNBOUNDED" and \
                    en_side == "FOLLOWING":
                frame = "whole"
            elif kind == "RANGE" and st_s.upper() == "UNBOUNDED" and st_side == "PRECEDING" and \
                    en_s.upper() == "CURRENT ROW":
                frame = "default"
            elif kind == "ROWS" and st_s.upper() == "UNBOUNDED" and st_side == "PRECEDING" and \
                    en_s.upper() == "CURRENT ROW":
                frame = "rows_to_current"
            else:
                raise Unsupported("window_frame", spec.sql())
        if isinstance(f, exp.RowNumber):
            return ir.Win("row_number", (), part, order, frame, INT)
        if isinstance(f, exp.Anonymous) and f.name.lower() in ("rank", "dense_rank", "row_number"):
            return ir.Win(f.name.lower(), (), part, order, frame, INT)
        if type(f).__name__ in ("Rank", "DenseRank"):
            return ir.Win("rank" if type(f).__name__ == "Rank" else "dense_rank", (), part, order, frame, INT)
        if type(f) in _AGG_NAMES:
            ag = self.agg(f, ctx)
            if ag.distinct and ag.func not in ("count", "sum", "avg", "min", "max"):
                raise Unsupported("window_distinct", ag.func)
            if ag.filt is not None:
                raise Unsupported("window_filter")
            return ir.Win(ag.func + ("_distinct" if ag.distinct else ""), ag.args, part, order, frame, ag.ty)
        raise Unsupported("window_func", type(f).__name__)

    # .................................................................. scalar expressions
    def bool_expr(self, node, ctx):
        e = self.expr(node, ctx)
        if e.ty == NULL:
            return lit(None, BOOL)
        if e.ty != BOOL:
            if e.ty == INT:          # MySQL/SQLite truthiness
                return fn("neq", [e, lit(0, INT)], BOOL)
            raise Unsupported("type", f"non-boolean predicate {node.sql()[:60]}")
        return e

    def resolve(self, c: exp.Column, ctx):
        table = c.table.lower() if c.table else None
        name = c.name.lower()
        for d, sc in enumerate(reversed(ctx.scopes)):
            e = sc.lookup(table, name)
            if e is not None:
                if d == 0:
                    return e
                return E("outer", (d, e.args[0]), e.ty)
        raise Unsupported("resolve", f"column {c.sql()}")

    def expr(self, n, ctx) -> E:
        k = _k(n)
        if ctx.subst and k in ctx.subst:
            return ctx.subst[k]
        if ctx.windows and isinstance(n, exp.Window) and k in ctx.windows:
            i, spec = ctx.windows[k]
            return E("win", (i,), spec.ty)
        m = getattr(self, "x_" + type(n).__name__, None)
        if m is None:
            raise Unsupported("expr", type(n).__name__)
        return m(n, ctx)

    # leaves
    def x_Column(self, n, ctx):
        if isinstance(n.this, exp.Star):
            raise Unsupported("resolve", "star")
        return self.resolve(n, ctx)

    def x_Paren(self, n, ctx):
        return self.expr(n.this, ctx)

    def x_Alias(self, n, ctx):
        return self.expr(n.this, ctx)

    def x_Null(self, n, ctx):
        return lit(None, NULL)

    def x_Boolean(self, n, ctx):
        return lit(bool(n.this), BOOL)

    def x_Literal(self, n, ctx):
        if n.is_string:
            return lit(n.this, STR)
        s = n.this
        try:
            if "." in s or "e" in s.lower():
                d = decimal.Decimal(s)
                if "e" in s.lower():
                    return lit(float(s), FLOAT)
                scale = max(0, -d.as_tuple().exponent)
                return lit(d, ir.DEC(scale))
            return lit(int(s), INT)
        except Exception:
            raise Unsupported("literal", s)

    def x_Neg(self, n, ctx):
        e = self.expr(n.this, ctx)
        if e.op == "lit" and e.args[0] is not None:
            return lit(-e.args[0], e.ty)
        _num(e, "neg")
        return fn("neg", [e], e.ty)

    def x_Placeholder(self, n, ctx):
        raise Unsupported("placeholder")

    # boolean connectives
    def x_And(self, n, ctx):
        return fn("and", [self.bool_expr(n.this, ctx), self.bool_expr(n.expression, ctx)], BOOL)

    def x_Or(self, n, ctx):
        return fn("or", [self.bool_expr(n.this, ctx), self.bool_expr(n.expression, ctx)], BOOL)

    def x_Not(self, n, ctx):
        return fn("not", [self.bool_expr(n.this, ctx)], BOOL)

    # comparisons
    def _cmp(self, op, n, ctx):
        rhs = n.expression
        if isinstance(rhs, (exp.Any, exp.All)):
            q = rhs.this
            q = q.this if isinstance(q, exp.Subquery) else q
            if not isinstance(q, exp.Query):
                raise Unsupported("any_all_nonquery")
            lhs = self.expr(n.this, ctx)
            rel = self.query(q, ctx.scopes)
            if rel.arity != 1:
                raise Unsupported("resolve", "ANY/ALL subquery arity")
            _comparable(lhs.ty, rel.types[0])
            return E("sub", ("cmp", rel, (lhs,), op, "any" if isinstance(rhs, exp.Any) else "all"), BOOL)
        a = self.expr(n.this, ctx)
        b = self.expr(rhs, ctx)
        a, b = _coerce_cmp(a, b)
        return fn(op, [a, b], BOOL)

    def x_EQ(self, n, ctx):
        return self._cmp("eq", n, ctx)

    def x_NEQ(self, n, ctx):
        return self._cmp("neq", n, ctx)

    def x_LT(self, n, ctx):
        return self._cmp("lt", n, ctx)

    def x_LTE(self, n, ctx):
        return self._cmp("le", n, ctx)

    def x_GT(self, n, ctx):
        return self._cmp("gt", n, ctx)

    def x_GTE(self, n, ctx):
        return self._cmp("ge", n, ctx)

    def x_NullSafeEQ(self, n, ctx):
        a, b = _coerce_cmp(self.expr(n.this, ctx), self.expr(n.expression, ctx))
        return fn("nseq", [a, b], BOOL)

    def x_NullSafeNEQ(self, n, ctx):
        a, b = _coerce_cmp(self.expr(n.this, ctx), self.expr(n.expression, ctx))
        return fn("not", [fn("nseq", [a, b], BOOL)], BOOL)

    def x_Is(self, n, ctx):
        a = self.expr(n.this, ctx)
        b = n.expression
        if isinstance(b, exp.Null):
            result = fn("is_null", [a], BOOL)
        elif isinstance(b, exp.Boolean):
            if a.ty != BOOL:
                raise Unsupported("type", "IS TRUE on non-boolean")
            result = fn("is_true" if b.this else "is_false", [a], BOOL)
        else:
            raise Unsupported("is", b.sql())
        return fn("not", [result], BOOL) if n.args.get("negate") else result

    def x_Between(self, n, ctx):
        a = self.expr(n.this, ctx)
        lo = self.expr(n.args["low"], ctx)
        hi = self.expr(n.args["high"], ctx)
        if n.args.get("symmetric"):
            raise Unsupported("between_symmetric")
        a1, lo = _coerce_cmp(a, lo)
        a2, hi = _coerce_cmp(a, hi)
        return fn("and", [fn("ge", [a1, lo], BOOL), fn("le", [a2, hi], BOOL)], BOOL)

    def x_In(self, n, ctx):
        q = n.args.get("query")
        if n.args.get("unnest") or n.args.get("field"):
            raise Unsupported("in_kind")
        lhs_node = n.this
        lhs = [self.expr(x, ctx) for x in lhs_node.expressions] if isinstance(lhs_node, exp.Tuple) \
            else [self.expr(lhs_node, ctx)]
        if q is not None:
            qq = q.this if isinstance(q, exp.Subquery) else q
            rel = self.query(qq, ctx.scopes)
            if rel.arity != len(lhs):
                raise Unsupported("resolve", "IN subquery arity")
            for a, t in zip(lhs, rel.types):
                _comparable(a.ty, t)
            return E("sub", ("cmp", rel, tuple(lhs), "eq", "any"), BOOL)
        items = n.expressions
        if len(items) == 1 and isinstance(items[0], (exp.Subquery, exp.Select)):
            qq = items[0].this if isinstance(items[0], exp.Subquery) else items[0]
            rel = self.query(qq, ctx.scopes)
            if rel.arity != len(lhs):
                raise Unsupported("resolve", "IN subquery arity")
            return E("sub", ("cmp", rel, tuple(lhs), "eq", "any"), BOOL)
        if len(lhs) != 1:
            raise Unsupported("in_tuple_list")
        ors = []
        for it in items:
            a, b = _coerce_cmp(lhs[0], self.expr(it, ctx))
            ors.append(fn("eq", [a, b], BOOL))
        return _or(ors)

    def x_Exists(self, n, ctx):
        q = n.this
        q = q.this if isinstance(q, exp.Subquery) else q
        rel = self.query(q, ctx.scopes)
        return E("sub", ("exists", rel, (), None, None), BOOL)

    def x_Subquery(self, n, ctx):
        rel = self.query(n.this, ctx.scopes)
        if n.args.get("order") or n.args.get("limit"):
            rel = self._order_limit(n, rel, ctx.scopes, False)
        if rel.arity != 1:
            raise Unsupported("resolve", "scalar subquery arity")
        return E("sub", ("scalar", rel, (), None, None), rel.types[0])

    def x_Like(self, n, ctx):
        return self._like(n, ctx, ci=self.dialect in ("sqlite", "mysql"))

    def x_ILike(self, n, ctx):
        return self._like(n, ctx, ci=True)

    def _like(self, n, ctx, ci):
        a = self.expr(n.this, ctx)
        p = n.expression
        esc = None
        if isinstance(p, exp.Escape):
            esc = p.expression.this
            p = p.this
        pat = self.expr(p, ctx)
        if pat.op != "lit" or pat.ty != STR:
            raise Unsupported("like_nonconst_pattern")
        if a.ty != STR:
            raise Unsupported("type", "LIKE on non-string")
        result = fn("like", [a, pat, lit(esc, STR), lit(ci, BOOL)], BOOL)
        return fn("not", [result], BOOL) if n.args.get("negate") else result

    def x_Escape(self, n, ctx):
        raise Unsupported("escape")

    def x_RegexpLike(self, n, ctx):
        """Regex with a constant pattern in the LIKE-expressible subset (anchors, literals, '.', '.*') is
        translated to LIKE exactly; anything else is Unsupported."""
        a = self.expr(n.this, ctx)
        p = self.expr(n.expression, ctx)
        if p.op != "lit" or p.ty != STR or a.ty != STR:
            raise Unsupported("regexp", "non-constant pattern or non-string operand")
        if n.args.get("flag") is not None:
            raise Unsupported("regexp", "flags")
        return _or_likes(a, _regex_to_likes(p.args[0]), self.dialect == "mysql")   # MySQL REGEXP: ci collation

    def x_RegexpILike(self, n, ctx):
        a = self.expr(n.this, ctx)
        p = self.expr(n.expression, ctx)
        if p.op != "lit" or p.ty != STR or a.ty != STR:
            raise Unsupported("regexp", "non-constant pattern or non-string operand")
        return _or_likes(a, _regex_to_likes(p.args[0]), True)

    def x_RegexpFullMatch(self, n, ctx):
        a = self.expr(n.this, ctx)
        p = self.expr(n.expression, ctx)
        if p.op != "lit" or p.ty != STR or a.ty != STR:
            raise Unsupported("regexp", "non-constant pattern or non-string operand")
        pat = p.args[0]
        core = pat[1:] if pat.startswith("^") else pat
        core = core[:-1] if core.endswith("$") and not core.endswith("\\$") else core
        depth, top_alt = 0, False
        for ch in core:
            depth += ch == "("
            depth -= ch == ")"
            top_alt |= ch == "|" and depth == 0
        pat = "^" + ("(" + core + ")" if top_alt else core) + "$"
        return _or_likes(a, _regex_to_likes(pat), False)

    def _trunc(self, n, ctx):
        e = self.expr(n.this, ctx)
        unit = n.args.get("unit")
        u = (unit.name if unit is not None else "").lower().strip("'")
        if u not in ("year", "month", "day"):
            raise Unsupported("date_trunc_unit", u)
        if e.ty != DATE:
            raise Unsupported("date_trunc", f"on {e.ty}")
        d = e if u == "day" else fn("date_trunc", [e, lit(u, STR)], DATE)
        return fn("date_ts", [d], TS)        # DATE_TRUNC on a DATE yields TIMESTAMP (DuckDB, PostgreSQL)

    def x_TimestampTrunc(self, n, ctx):
        return self._trunc(n, ctx)

    def x_DateTrunc(self, n, ctx):
        return self._trunc(n, ctx)

    # arithmetic
    def _arith(self, op, n, ctx):
        a = self.expr(n.this, ctx)
        b = self.expr(n.expression, ctx)
        # date +/- interval, date - date
        if op in ("add", "sub") and (a.ty in (DATE, TS) or b.ty in (DATE, TS) or "interval" in (a.ty, b.ty)):
            return self._date_arith(op, a, b)
        if a.ty == NULL or b.ty == NULL:
            return lit(None, NULL)
        _num(a, op), _num(b, op)
        if op == "div":
            ty = self._div_type(a.ty, b.ty)
            if ty == INT:
                return fn("idiv", [a, b], INT)
            return fn("div", [a, b], ty)
        if op == "mod":
            return fn("mod", [a, b], _arith_type(a.ty, b.ty, "add"))
        if a.op == "lit" and b.op == "lit" and op != "div":
            pass
        return fn(op, [a, b], _arith_type(a.ty, b.ty, op))

    def _div_type(self, ta, tb):
        if ta == INT and tb == INT:
            return FLOAT if self.dialect == "duckdb" else (ir.DEC(4) if self.dialect == "mysql" else INT)
        return FLOAT

    def _date_arith(self, op, a, b):
        if a.ty == "interval" and op == "add":
            a, b = b, a
        if a.ty in (DATE, TS) and b.ty == "interval":
            n, unit = b.args[0]
            if op == "sub":
                n = -n
            if a.op == "lit":
                d = a.args[0]
                if unit == "day":
                    return lit(d + dt.timedelta(days=n), a.ty)
                if unit == "month":
                    return lit(add_months(d, n), a.ty)
                if unit == "year":
                    return lit(add_months(d, 12 * n), a.ty)
                raise Unsupported("interval_unit", unit)
            if a.ty == TS:
                raise Unsupported("timestamp_arith")
            if unit == "day":
                return fn("date_add_days", [a, lit(n, INT)], DATE)
            if unit in ("month", "year"):
                return fn("date_add_months", [a, lit(n * (12 if unit == "year" else 1), INT)], DATE)
            raise Unsupported("interval_unit", unit)
        if a.ty == DATE and b.ty == DATE and op == "sub":
            return fn("date_diff_days", [a, b], INT)
        if a.ty == DATE and b.ty == INT and self.dialect in ("postgres", "duckdb"):
            return fn("date_add_days", [a, b if op == "add" else fn("neg", [b], INT)], DATE)
        raise Unsupported("date_arith", f"{a.ty} {op} {b.ty}")

    def x_Add(self, n, ctx):
        return self._arith("add", n, ctx)

    def x_Sub(self, n, ctx):
        return self._arith("sub", n, ctx)

    def x_Mul(self, n, ctx):
        return self._arith("mul", n, ctx)

    def x_Div(self, n, ctx):
        if n.args.get("typed") or n.args.get("safe"):
            pass
        return self._arith("div", n, ctx)

    def x_IntDiv(self, n, ctx):
        a, b = self.expr(n.this, ctx), self.expr(n.expression, ctx)
        _num(a, "idiv"), _num(b, "idiv")
        return fn("idiv", [a, b], INT)

    def x_Mod(self, n, ctx):
        return self._arith("mod", n, ctx)

    def x_DPipe(self, n, ctx):
        return self._concat([n.this, n.expression], ctx)

    def x_Concat(self, n, ctx):
        return self._concat(n.expressions, ctx)

    def _concat(self, parts, ctx):
        es = [self.expr(p, ctx) for p in parts]
        for e in es:
            if e.ty not in (STR, NULL):
                raise Unsupported("concat_type", e.ty)
        # fold constants, then allow at most one non-constant string
        out = None
        for e in es:
            if e.ty == NULL:
                return lit(None, STR)
            if out is None:
                out = e
            elif out.op == "lit" and e.op == "lit":
                out = lit(out.args[0] + e.args[0], STR)
            elif e.op == "lit":
                out = fn("strmap", [out, lit(("concat_r", e.args[0]), "params")], STR)
            elif out.op == "lit":
                out = fn("strmap", [e, lit(("concat_l", out.args[0]), "params")], STR)
            else:
                raise Unsupported("concat_two_columns")
        return out

    def x_Interval(self, n, ctx):
        v = n.this
        unit = n.args.get("unit")
        unit_s = (unit.name if unit is not None else "").lower()
        txt = v.this if isinstance(v, exp.Literal) else None
        if txt is None:
            raise Unsupported("interval_expr")
        parts = str(txt).strip().split()
        if len(parts) == 2 and not unit_s:
            txt, unit_s = parts[0], parts[1].lower()
        unit_s = unit_s.rstrip("s")
        try:
            num = int(str(txt).strip())
        except ValueError:
            raise Unsupported("interval_expr", str(txt))
        if unit_s not in ("day", "month", "year"):
            raise Unsupported("interval_unit", unit_s)
        return lit((num, unit_s), "interval")

    # casts and conditionals
    def x_Cast(self, n, ctx):
        to = n.args["to"]
        e = self.expr(n.this, ctx)
        t = to.this
        T = exp.DataType.Type
        if t in (T.DATE,):
            if e.op == "lit" and e.ty == STR:
                try:
                    return lit(dt.date.fromisoformat(e.args[0].strip()[:10]), DATE)
                except ValueError:
                    raise Unsupported("cast", f"bad date literal {e.args[0]}")
            if e.ty == DATE:
                return e
            if e.ty == TS:
                return fn("ts_date", [e], DATE)
            raise Unsupported("cast", f"{e.ty} -> date")
        if t in (T.TIMESTAMP, T.DATETIME, T.TIMESTAMPNTZ):
            if e.op == "lit" and e.ty == STR:
                try:
                    return lit(dt.datetime.fromisoformat(e.args[0].strip()), TS)
                except ValueError:
                    raise Unsupported("cast", "bad timestamp literal")
            if e.ty == TS:
                return e
            raise Unsupported("cast", f"{e.ty} -> timestamp")
        from seer2.schema import map_type
        ty, param = map_type(to)
        if ty in ("int", "dec", "float"):
            if e.ty == STR:
                if e.op == "lit":
                    try:
                        return self.x_Literal(exp.Literal.number(e.args[0].strip()), ctx) if ty != "int" \
                            else lit(int(e.args[0].strip()), INT)
                    except Exception:
                        raise Unsupported("cast", "string literal -> number")
                raise Unsupported("cast", "string column -> number")
            if e.ty == BOOL:
                one = lit(1, INT) if ty == "int" else self.x_Literal(exp.Literal.number("1"), ctx)
                zero = lit(0, INT) if ty == "int" else self.x_Literal(exp.Literal.number("0"), ctx)
                b = _case([(e, one), (fn("not", [e], BOOL), zero)], lit(None, NULL))
                if ty == "int":
                    return b
                e = b
            if e.ty == NULL:
                return lit(None, NULL)
            target = INT if ty == "int" else (FLOAT if ty == "float" else ir.DEC((param or (18, 3))[1]))
            if target == e.ty:
                return e
            return fn("cast_num", [e, lit(target, "type")], target)
        if ty == "str":
            if e.ty == STR or e.ty == NULL:
                return e
            raise Unsupported("cast", f"{e.ty} -> string")
        if ty == "bool":
            if e.ty == BOOL:
                return e
            raise Unsupported("cast", f"{e.ty} -> bool")
        raise Unsupported("cast", to.sql())

    def x_TryCast(self, n, ctx):
        return self.x_Cast(n, ctx)

    def x_Case(self, n, ctx):
        base = n.this
        whens = []
        b = self.expr(base, ctx) if base is not None else None
        for w in n.args.get("ifs") or []:
            if b is not None:
                x, y = _coerce_cmp(b, self.expr(w.this, ctx))
                c = fn("eq", [x, y], BOOL)
            else:
                c = self.bool_expr(w.this, ctx)
            whens.append((c, self.expr(w.args["true"], ctx)))
        dflt = n.args.get("default")
        el = self.expr(dflt, ctx) if dflt is not None else lit(None, NULL)
        return _case(whens, el)

    def x_If(self, n, ctx):
        c = self.bool_expr(n.this, ctx)
        t = self.expr(n.args["true"], ctx)
        f = n.args.get("false")
        el = self.expr(f, ctx) if f is not None else lit(None, NULL)
        return _case([(c, t)], el)

    def x_Coalesce(self, n, ctx):
        es = [self.expr(n.this, ctx)] + [self.expr(x, ctx) for x in n.expressions]
        ty = _unify_all([e.ty for e in es])
        return fn("coalesce", [_retype(e, ty) for e in es], ty)

    def x_Nullif(self, n, ctx):
        a = self.expr(n.this, ctx)
        b = self.expr(n.expression, ctx)
        x, y = _coerce_cmp(a, b)
        return _case([(fn("eq", [x, y], BOOL), lit(None, NULL))], a)

    # numeric functions
    def x_Abs(self, n, ctx):
        e = self.expr(n.this, ctx)
        _num(e, "abs")
        return fn("abs", [e], e.ty)

    def x_Round(self, n, ctx):
        e = self.expr(n.this, ctx)
        _num(e, "round")
        d = n.args.get("decimals")
        nd = 0
        if d is not None:
            de = self.expr(d, ctx)
            if de.op != "lit" or de.ty != INT:
                raise Unsupported("round_nonconst")
            nd = de.args[0]
        ty = e.ty if e.ty in (INT, FLOAT) else ir.DEC(max(0, min(nd, ir.dec_scale(e.ty))))
        return fn("round", [e, lit(nd, INT)], ty)

    def x_Floor(self, n, ctx):
        e = self.expr(n.this, ctx)
        _num(e, "floor")
        return fn("floor", [e], INT if e.ty == INT else (FLOAT if e.ty == FLOAT else ir.DEC(0)))

    def x_Ceil(self, n, ctx):
        e = self.expr(n.this, ctx)
        _num(e, "ceil")
        return fn("ceil", [e], INT if e.ty == INT else (FLOAT if e.ty == FLOAT else ir.DEC(0)))

    # string functions (finite-pool lookups)
    def _strmap(self, name, arg, params, ctx):
        e = self.expr(arg, ctx)
        if e.ty == NULL:
            return lit(None, STR)
        if e.ty != STR:
            raise Unsupported("type", f"{name} on {e.ty}")
        ps = []
        for p in params:
            if p is None:
                continue
            pe = self.expr(p, ctx)
            if pe.op != "lit":
                raise Unsupported("strfunc_nonconst", name)
            ps.append(pe.args[0])
        if e.op == "lit":
            v = STR_MAPS[name](e.args[0], *ps)
            return lit(v, STR) if v is not None else lit(None, STR)
        return fn("strmap", [e, lit((name, *ps), "params")], STR)

    def x_Upper(self, n, ctx):
        return self._strmap("upper", n.this, [], ctx)

    def x_Lower(self, n, ctx):
        return self._strmap("lower", n.this, [], ctx)

    def x_Trim(self, n, ctx):
        if n.args.get("expression") is not None or n.args.get("collation"):
            raise Unsupported("trim_chars")
        pos = (n.args.get("position") or "BOTH").upper()
        return self._strmap({"BOTH": "trim", "LEADING": "ltrim", "TRAILING": "rtrim"}[pos], n.this, [], ctx)

    def x_Substring(self, n, ctx):
        return self._strmap("substr", n.this, [n.args.get("start"), n.args.get("length")], ctx)

    def x_Left(self, n, ctx):
        return self._strmap("left", n.this, [n.expression], ctx)

    def x_Right(self, n, ctx):
        return self._strmap("right", n.this, [n.expression], ctx)

    def x_Replace(self, n, ctx):
        return self._strmap("replace", n.this, [n.expression, n.args.get("replacement")], ctx)

    def x_Length(self, n, ctx):
        e = self.expr(n.this, ctx)
        if e.ty != STR:
            raise Unsupported("type", "length")
        if e.op == "lit":
            return lit(len(e.args[0]), INT)
        return fn("strnum", [e, lit(("length",), "params")], INT)

    def x_StrPosition(self, n, ctx):
        e = self.expr(n.this, ctx)
        sub = self.expr(n.args["substr"], ctx)
        if sub.op != "lit" or n.args.get("position") is not None:
            raise Unsupported("strfunc_nonconst", "instr")
        return fn("strnum", [e, lit(("instr", sub.args[0]), "params")], INT)

    # dates
    def x_Extract(self, n, ctx):
        unit = n.this.name.lower() if isinstance(n.this, (exp.Var, exp.Identifier)) else str(n.this).lower()
        e = self.expr(n.expression, ctx)
        return self._date_part(unit, e)

    def _date_part(self, unit, e):
        if e.ty not in (DATE, TS):
            raise Unsupported("type", f"extract from {e.ty}")
        if unit not in ("year", "month", "day"):
            raise Unsupported("extract_unit", unit)
        if e.op == "lit":
            return lit(getattr(e.args[0], unit), INT)
        if e.ty == TS:
            raise Unsupported("timestamp_extract")
        return fn("date_part", [e, lit(unit, STR)], INT)

    def x_Year(self, n, ctx):
        return self._date_part("year", self.expr(n.this, ctx))

    def x_Month(self, n, ctx):
        return self._date_part("month", self.expr(n.this, ctx))

    def x_Day(self, n, ctx):
        return self._date_part("day", self.expr(n.this, ctx))

    def x_TimeToStr(self, n, ctx):
        # SQLite strftime('%Y', d) etc.
        f = n.args.get("format")
        fmt = f.this if isinstance(f, exp.Literal) else None
        e = self.expr(n.this, ctx)
        if fmt in ("%Y", "%m", "%d") and e.ty in (DATE, TS, STR):
            if e.ty == STR:
                raise Unsupported("date_from_string_column")
            raise Unsupported("strftime_to_string", fmt)
        raise Unsupported("strftime", str(fmt))

    def x_CurrentDate(self, n, ctx):
        raise Unsupported("current_date")

    def x_CurrentTimestamp(self, n, ctx):
        raise Unsupported("current_timestamp")

    def x_Anonymous(self, n, ctx):
        name = n.name.lower()
        args = n.expressions
        if name in ("iif",) and len(args) == 3:
            return _case([(self.bool_expr(args[0], ctx), self.expr(args[1], ctx))], self.expr(args[2], ctx))
        if name == "ifnull" and len(args) == 2:
            es = [self.expr(a, ctx) for a in args]
            ty = _unify_all([e.ty for e in es])
            return fn("coalesce", [_retype(e, ty) for e in es], ty)
        if name in ("substr", "substring"):
            return self._strmap("substr", args[0], args[1:3], ctx)
        raise Unsupported("function", name)

    def x_Window(self, n, ctx):
        raise Unsupported("window_position", "window function outside SELECT/ORDER BY")

    def x_Tuple(self, n, ctx):
        raise Unsupported("tuple_expr")

    def x_Star(self, n, ctx):
        raise Unsupported("resolve", "star")

    def x_Distinct(self, n, ctx):
        raise Unsupported("distinct_expr")

    def __getattr__(self, name):
        if name.startswith("x_") and name[2:] in _AGG_TYPE_NAMES:
            def f(n, ctx):
                raise Unsupported("agg_position", name[2:])
            return f
        raise AttributeError(name)


# ---------------------------------------------------------------------- helpers
class Ctx:
    __slots__ = ("scopes", "subst", "windows")

    def __init__(self, scopes, subst=None):
        self.scopes = list(scopes)
        self.subst = subst or {}
        self.windows = None


_AGG_NAMES = {exp.Count: "count", exp.Sum: "sum", exp.Avg: "avg", exp.Min: "min", exp.Max: "max",
              exp.AnyValue: "any_value"}
_AGG_TYPE_NAMES = {c.__name__ for c in _AGG_NAMES} | {"AggFunc"}


def _k(n):
    return n.sql(dialect="duckdb") if n is not None else None


def _inside_subquery(node, root):
    if node is root:
        return False
    p = node.parent
    while p is not None and p is not root:
        if isinstance(p, (exp.Subquery, exp.Select)) and p is not root:
            return True
        p = p.parent
    return False


def _is_winfunc(a):
    p = a.parent
    if isinstance(p, exp.Filter):
        p = p.parent
    return isinstance(p, exp.Window) and (p.this is a or p.this is a.parent)


def _aggs_in(x):
    """Group aggregates in x: AggFunc nodes that are not window functions themselves (an aggregate inside a
    window's argument, PARTITION BY or ORDER BY, as in sum(sum(x)) OVER (...), is a group aggregate)."""
    out = []
    for a in x.find_all(exp.AggFunc):
        if _inside_subquery(a, x) or _is_winfunc(a) or isinstance(a, exp.Grouping):
            continue
        if any(isinstance(p, exp.AggFunc) and not _is_winfunc(p) for p in _ancestors(a, x)):
            raise Unsupported("nested_aggregate")
        node = a.parent if isinstance(a.parent, exp.Filter) else a
        if type(a) not in _AGG_NAMES:
            raise Unsupported("agg_func", type(a).__name__)
        out.append(node)
    return out


def _grouping_sets(group):
    """GROUP BY items -> list of grouping sets (lists of key expressions), in SQL order."""
    sets = [[]]

    def unit(e):
        if isinstance(e, (exp.Tuple, exp.Paren)) and not isinstance(e, exp.Column):
            return list(e.expressions) if isinstance(e, exp.Tuple) else [e.this]
        return [e]
    for g in group.expressions:
        if isinstance(g, exp.Rollup):
            units = [unit(e) for e in g.expressions]
            alts = [sum(units[:i], []) for i in range(len(units), -1, -1)]
        elif isinstance(g, exp.Cube):
            units = [unit(e) for e in g.expressions]
            n = len(units)
            masks = sorted(range(2 ** n), key=lambda m: (-bin(m).count("1"), -m))
            alts = [sum([units[i] for i in range(n) if m >> (n - 1 - i) & 1], []) for m in masks]
        elif isinstance(g, exp.GroupingSets):
            alts = [unit(e) if not (isinstance(e, exp.Tuple) and not e.expressions) else [] for e in g.expressions]
        else:
            alts = [[g]]
        sets = [s + a for s in sets for a in alts]
    if not sets or len(sets) > 64:
        raise Unsupported("grouping_sets", f"{len(sets)} sets")
    return sets


def _expand_grouping_sets(sel):
    """SELECT ... GROUP BY ROLLUP/CUBE/GROUPING SETS -> an equivalent query without them:
    one branch per grouping set computes the group row's atoms (all keys, with the keys outside the set as NULL;
    every group aggregate; every GROUPING(...) as a constant; and count(*) so that the empty set yields one row);
    HAVING is applied per branch; the branches are combined by UNION ALL; the original select list, windows,
    DISTINCT, ORDER BY and LIMIT are evaluated over that union (windows therefore span all grouping sets)."""
    group = sel.args["group"]
    sets = _grouping_sets(group)
    keys = []
    for st in sets:
        for k in st:
            if _k(k) not in [_k(x) for x in keys]:
                keys.append(k)
    kidx = {_k(k): i for i, k in enumerate(keys)}
    order = sel.args.get("order")
    roots = list(sel.expressions) + ([o.this for o in order.expressions] if order is not None else [])
    aggs, groupings = [], []
    for x in roots:
        for a in _aggs_in(x):
            if _k(a) not in [_k(y) for y in aggs]:
                aggs.append(a)
        for g in x.find_all(exp.Grouping):
            if _k(g) not in [_k(y) for y in groupings]:
                groupings.append(g)
    having = sel.args.get("having")
    names = [f"_gk{i}" for i in range(len(keys))] + [f"_ga{i}" for i in range(len(aggs))] + \
        [f"_gg{i}" for i in range(len(groupings))] + ["_gcnt"]

    def gbits(g, st):
        ks = {_k(k) for k in st}
        v = 0
        for e in g.expressions:
            if _k(e) not in kidx:
                raise Unsupported("grouping_sets", f"GROUPING of non-key {_k(e)}")
            v = v * 2 + (0 if _k(e) in ks else 1)
        return v

    def in_branch(e, st):
        ks = {_k(k) for k in st}

        def f(n):
            if isinstance(n, exp.Grouping):
                return exp.Literal.number(gbits(n, st))
            if isinstance(n, exp.AggFunc) and not _is_winfunc(n):
                return n.copy()
            if _k(n) in kidx and _k(n) not in ks:
                return exp.Null()
            return n
        return e.copy().transform(f)

    branches = []
    for st in sets:
        b = sel.copy()
        for a in ("order", "limit", "offset", "distinct", "group", "having"):
            b.set(a, None)
        items = [exp.alias_(k.copy() if _k(k) in {_k(x) for x in st} else exp.Null(), names[i])
                 for i, k in enumerate(keys)]
        items += [exp.alias_(a.copy(), names[len(keys) + i]) for i, a in enumerate(aggs)]
        items += [exp.alias_(exp.Literal.number(gbits(g, st)), names[len(keys) + len(aggs) + i])
                  for i, g in enumerate(groupings)]
        items.append(exp.alias_(exp.Count(this=exp.Star()), "_gcnt"))
        b.set("expressions", items)
        if st:
            b.set("group", exp.Group(expressions=[k.copy() for k in st]))
        if having is not None:
            b.set("having", exp.Having(this=in_branch(having.this, st)))
        branches.append(b)
    u = branches[0]
    for b in branches[1:]:
        u = exp.Union(this=u, expression=b, distinct=False)
    ref = {}
    for i, k in enumerate(keys):
        ref[_k(k)] = names[i]
    for i, a in enumerate(aggs):
        ref[_k(a)] = names[len(keys) + i]
    for i, g in enumerate(groupings):
        ref[_k(g)] = names[len(keys) + len(aggs) + i]

    aliases = {p.alias.lower() for p in sel.expressions if isinstance(p, exp.Alias)}

    def outer(e, allow_alias=False):
        def f(n):
            r = ref.get(_k(n))
            if r is not None and not isinstance(n, exp.Window):
                return exp.column(r, table="_g")
            return n
        out = e.copy().transform(f)
        for c in out.find_all(exp.Column):
            if allow_alias and not c.table and c.name.lower() in aliases:
                continue
            if c.table != "_g" and not _inside_subquery(c, out):
                raise Unsupported("grouping_sets", f"non-key column {c.sql()} outside aggregates")
        return out

    o = exp.Select(expressions=[outer(p) for p in sel.expressions])
    o.set("from", exp.From(this=exp.Subquery(this=u, alias=exp.TableAlias(this=exp.to_identifier("_g")))))
    if order is not None:
        amap = {p.alias.lower(): p.this for p in sel.expressions if isinstance(p, exp.Alias)}
        oitems = []
        for x in order.expressions:
            if isinstance(x.this, exp.Column) and not x.this.table and x.this.name.lower() in amap:
                oitems.append(x.copy())          # a bare output alias is resolved by the block
                continue
            # an output alias inside a larger ORDER BY expression: inline its (pre-projection) definition
            x = x.copy().transform(lambda n: amap[n.name.lower()].copy()
                                   if isinstance(n, exp.Column) and not n.table and n.name.lower() in amap else n)
            oitems.append(outer(x))
        o.set("order", exp.Order(expressions=oitems))
    for a in ("limit", "offset", "distinct"):
        if sel.args.get(a) is not None:
            o.set(a, sel.args[a].copy())
    w = sel.args.get("with") or sel.args.get("with_")
    if w is not None:
        o.set("with", w.copy())
    return o


def _ancestors(n, root):
    p = n.parent
    while p is not None and p is not root:
        yield p
        p = p.parent
    if p is root and isinstance(root, exp.Window) and root is not n:
        yield root


def _num(e, what):
    if e.ty == NULL:
        return
    if not is_num(e.ty):
        raise Unsupported("type", f"{what} on {e.ty}")


def _arith_type(a, b, op):
    if a == NULL:
        return b
    if b == NULL:
        return a
    if a == FLOAT or b == FLOAT:
        return FLOAT
    if a == INT and b == INT:
        return INT
    sa = ir.dec_scale(a) if is_dec(a) else 0
    sb = ir.dec_scale(b) if is_dec(b) else 0
    if op == "mul":
        return ir.DEC(sa + sb)
    return ir.DEC(max(sa, sb))


def _unify(a, b):
    if a == b:
        return a
    if a == NULL:
        return b
    if b == NULL:
        return a
    if is_num(a) and is_num(b):
        return _arith_type(a, b, "add")
    if {a, b} == {DATE, TS}:
        return TS
    raise Unsupported("type", f"cannot unify {a} and {b}")


def _unify_all(ts):
    t = NULL
    for x in ts:
        t = _unify(t, x)
    return t


def _retype(e, ty):
    if e.ty == ty or e.ty == NULL and False:
        return e
    if e.op == "lit" and e.args[0] is None:
        return lit(None, ty)
    if is_num(e.ty) and is_num(ty):
        return fn("cast_num", [e, lit(ty, "type")], ty) if e.ty != ty else e
    return e


def _comparable(a, b):
    if a == b or NULL in (a, b) or (is_num(a) and is_num(b)) or {a, b} == {DATE, TS}:
        return
    if STR in (a, b) and (DATE in (a, b) or TS in (a, b)):
        return
    raise Unsupported("type", f"compare {a} with {b}")


def _regex_to_like(pat):
    out, i = [], 0
    start = pat.startswith("^")
    if start:
        i = 1
    end = pat.endswith("$") and not pat.endswith("\\$")
    body = pat[i:len(pat) - 1 if end else len(pat)]
    j = 0
    while j < len(body):
        c = body[j]
        if c == "." and body[j + 1:j + 2] == "*":
            out.append("%"); j += 2; continue
        if c == ".":
            out.append("_"); j += 1; continue
        if c == "\\" and j + 1 < len(body) and not body[j + 1].isalnum():
            c = body[j + 1]; j += 1
        elif c in "[](){}+*?|^$\\":
            raise Unsupported("regexp", f"pattern {pat!r}")
        out.append("\\" + c if c in "%_\\" else c)
        j += 1
    s = "".join(out)
    return ("" if start else "%") + s + ("" if end else "%")


def _regex_to_likes(pat):
    """Regex -> list of LIKE patterns whose disjunction matches the same strings (search semantics). Handles
    anchors, literals, '.', '.*', and one group of literal alternatives (a|b|c) with optional anchors around it."""
    import re as _re
    m = _re.fullmatch(r"(\^?)(.*?)\(([^()\[\]{}*+?.\\^$]*(?:\|[^()\[\]{}*+?.\\^$|]*)+)\)(.*?)(\$?)", pat)
    if m and not pat.endswith("\\$"):
        pre, left, alts, right, post = m.groups()
        return [_regex_to_like(pre + left + alt + right + post) for alt in alts.split("|")]
    if pat.startswith("^(") and pat.endswith(")$") and "|" not in pat:
        pat = "^" + pat[2:-2] + "$"
    return [_regex_to_like(pat)]


def _or_likes(a, pats, ci):
    xs = [fn("like", [a, lit(p, STR), lit("\\", STR), lit(ci, BOOL)], BOOL) for p in pats]
    out = xs[0]
    for x in xs[1:]:
        out = fn("or", [out, x], BOOL)
    return out


def _coerce_cmp(a, b):
    """Implicit casts in comparisons: string literal vs date column -> date literal."""
    for x, y, flip in ((a, b, False), (b, a, True)):
        if x.ty in (DATE, TS) and y.ty == STR:
            if y.op != "lit":
                raise Unsupported("type", "date compared with string column")
            try:
                v = dt.date.fromisoformat(y.args[0].strip()[:10]) if x.ty == DATE \
                    else dt.datetime.fromisoformat(y.args[0].strip())
            except ValueError:
                raise Unsupported("type", "bad date string")
            y2 = lit(v, x.ty)
            return (y2, x) if flip else (x, y2)
    if a.ty == DATE and b.ty == TS:
        a = fn("date_ts", [a], TS)
    if a.ty == TS and b.ty == DATE:
        b = fn("date_ts", [b], TS)
    _comparable(a.ty, b.ty)
    if a.ty == NULL and b.ty != NULL:
        a = lit(None, b.ty)
    if b.ty == NULL and a.ty != NULL:
        b = lit(None, a.ty)
    return a, b


def _and(xs):
    xs = list(xs)
    if not xs:
        return ir.TRUE
    out = xs[0]
    for x in xs[1:]:
        out = fn("and", [out, x], BOOL)
    return out


def _or(xs):
    out = xs[0]
    for x in xs[1:]:
        out = fn("or", [out, x], BOOL)
    return out


def _case(whens, el):
    ty = _unify_all([r.ty for _, r in whens] + [el.ty])
    if ty == NULL:
        ty = NULL
    return E("case", (tuple((c, _retype(r, ty)) for c, r in whens), _retype(el, ty)), ty)


def _shift_win(e, nb):
    """Replace E('win', (i,)) by a column reference nb+i into the window operator's output."""
    if not isinstance(e, E):
        return e
    if e.op == "win":
        return ir.col(nb + e.args[0], e.ty)
    if e.op in ("col", "outer", "lit"):
        return e
    if e.op == "sub":
        k, rel, lhs, op, q = e.args
        return E("sub", (k, rel, tuple(_shift_win(x, nb) for x in lhs), op, q), e.ty)
    if e.op == "case":
        whens, el = e.args
        return E("case", (tuple((_shift_win(c, nb), _shift_win(r, nb)) for c, r in whens), _shift_win(el, nb)),
                 e.ty)
    if e.op == "fn":
        return E("fn", (e.args[0], *[_shift_win(a, nb) for a in e.args[1:]]), e.ty)
    return e
