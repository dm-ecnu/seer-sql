"""SEER intermediate representation: a typed relational algebra with de Bruijn column references.

Relational nodes (class R) and scalar expressions (class E) are immutable and compared structurally; their hash is
cached, so equal sub-plans of different queries of a batch are the same dictionary key (sub-expression sharing by
structural hash, used by the encoder's memo table).

Column references are positional and scope-relative:
  E("col", (i,))        column i of the current input row of the enclosing operator;
  E("outer", (d, i))    column i of the input row d levels further out (correlated subqueries; d >= 1).
Hence two queries that differ only in table/column aliases produce identical IR (alpha-invariance).

Value types (E.ty / R.types): int, dec(scale), float, str, date, ts, bool, null.

Relational operators (R.op, R.args):
  scan      (table,)
  filter    (child, pred)
  project   (child, (expr, ...))
  join      (kind, left, right, cond)          kind in inner|left|right|full|cross|semi|anti
  agg       (child, (key, ...), (Agg, ...))    output = keys ++ aggregates; no keys = scalar aggregate (1 row)
  window    (child, (Win, ...))                output = child columns ++ window results
  distinct  (child,)
  setop     (kind, all, left, right)           kind in union|intersect|except
  sort      (child, ((expr, desc, nulls_first), ...), limit, offset)   limit/offset: int or None
  values    (((E, ...), ...),)                  constant rows

Scalar operators (E.op): col, outer, lit, fn (name + args), case, sub (subquery), agg results are columns of the agg
operator. Every construct the front end cannot map raises Unsupported(code, detail); nothing is silently dropped.
"""
from __future__ import annotations

INT, FLOAT, STR, DATE, TS, BOOL, NULL = "int", "float", "str", "date", "ts", "bool", "null"


def DEC(scale):
    return f"dec{scale}"


def is_dec(t):
    return isinstance(t, str) and t.startswith("dec")


def dec_scale(t):
    return int(t[3:])


def is_num(t):
    return t in (INT, FLOAT) or is_dec(t)


class Unsupported(Exception):
    """A construct outside SEER's fragment. code is a short stable tag used for coverage statistics."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class _Node:
    __slots__ = ("op", "args", "_h")

    def __eq__(self, other):
        return self is other or (type(self) is type(other) and self._h == other._h and self.op == other.op
                                 and self.args == other.args and self._extra() == other._extra())

    def __hash__(self):
        return self._h

    def _extra(self):
        return None


class E(_Node):
    __slots__ = ("ty",)

    def __init__(self, op, args, ty):
        self.op, self.args, self.ty = op, tuple(args), ty
        self._h = hash(("E", op, self.args, ty))

    def _extra(self):
        return self.ty

    def __repr__(self):
        if self.op == "col":
            return f"#{self.args[0]}"
        if self.op == "outer":
            return f"^{self.args[0]}#{self.args[1]}"
        if self.op == "lit":
            return repr(self.args[0])
        return f"{self.op}{list(self.args)}:{self.ty}"


class R(_Node):
    __slots__ = ("types", "names", "_free")

    def __init__(self, op, args, types, names=None):
        self.op, self.args, self.types = op, tuple(args), tuple(types)
        self.names = tuple(names) if names else tuple(f"c{i}" for i in range(len(self.types)))
        self._h = hash(("R", op, self.args, self.types))
        self._free = None

    def _extra(self):
        return self.types

    @property
    def arity(self):
        return len(self.types)

    def __repr__(self):
        return f"{self.op}({', '.join(map(repr, self.args))})"


class Agg:
    """Aggregate call: func in count|count_star|sum|avg|min|max|any_value; args are exprs over the agg input row."""
    __slots__ = ("func", "distinct", "args", "filt", "ty", "_h")

    def __init__(self, func, distinct, args, ty, filt=None):
        self.func, self.distinct, self.args, self.ty, self.filt = func, bool(distinct), tuple(args), ty, filt
        self._h = hash(("Agg", func, self.distinct, self.args, ty, filt))

    def __eq__(self, o):
        return isinstance(o, Agg) and self._h == o._h and (self.func, self.distinct, self.args, self.ty, self.filt) == \
            (o.func, o.distinct, o.args, o.ty, o.filt)

    def __hash__(self):
        return self._h

    def __repr__(self):
        return f"{self.func}{'(DISTINCT)' if self.distinct else ''}{list(self.args)}"


class Win:
    """Window function over the input row: func in row_number|rank|dense_rank|count|count_star|sum|avg|min|max;
    partition: exprs; order: ((expr, desc, nulls_first), ...); frame: 'default' (RANGE UNBOUNDED PRECEDING ..
    CURRENT ROW when ordered, whole partition otherwise) or 'whole'."""
    __slots__ = ("func", "args", "partition", "order", "frame", "ty", "_h")

    def __init__(self, func, args, partition, order, frame, ty):
        self.func, self.args, self.partition, self.order, self.frame, self.ty = \
            func, tuple(args), tuple(partition), tuple(order), frame, ty
        self._h = hash(("Win", func, self.args, self.partition, self.order, frame, ty))

    def __eq__(self, o):
        return isinstance(o, Win) and self._h == o._h and \
            (self.func, self.args, self.partition, self.order, self.frame, self.ty) == \
            (o.func, o.args, o.partition, o.order, o.frame, o.ty)

    def __hash__(self):
        return self._h

    def __repr__(self):
        return f"{self.func}{list(self.args)} over({list(self.partition)} order {list(self.order)})"


def intern_tree(node, pool):
    """Canonicalize immutable IR nodes across a stream, preserving output names.

    The Z3 encoding cache is specific to its finite value domain and must be
    rebuilt when that domain grows. This IR pool is independent of that domain.
    """
    if isinstance(node, tuple):
        return tuple(intern_tree(x, pool) for x in node)
    if isinstance(node, R):
        args = intern_tree(node.args, pool)
        value = node if all(a is b for a, b in zip(args, node.args)) else R(node.op, args, node.types, node.names)
        key = (R, value, value.names)
    elif isinstance(node, E):
        args = intern_tree(node.args, pool)
        value = node if all(a is b for a, b in zip(args, node.args)) else E(node.op, args, node.ty)
        key = (E, value)
    elif isinstance(node, Agg):
        args = intern_tree(node.args, pool)
        filt = intern_tree(node.filt, pool)
        value = Agg(node.func, node.distinct, args, node.ty, filt)
        key = (Agg, value)
    elif isinstance(node, Win):
        args = intern_tree(node.args, pool)
        partition = intern_tree(node.partition, pool)
        order = intern_tree(node.order, pool)
        value = Win(node.func, args, partition, order, node.frame, node.ty)
        key = (Win, value)
    else:
        return node
    return pool.setdefault(key, value)


# ---------------------------------------------------------------------- constructors
def col(i, ty):
    return E("col", (i,), ty)


def lit(v, ty):
    return E("lit", (v,), ty)


def fn(name, args, ty):
    return E("fn", (name, *args), ty)


TRUE = lit(True, BOOL)


# ---------------------------------------------------------------------- free outer references
def free_depth(x, d=0):
    """Largest outer level referenced by x that escapes x (0 = closed). For a relation, references inside its own
    operator expressions are relative to its input row (depth 0), and outer refs inside nested subqueries are
    shifted by one per nesting level."""
    if isinstance(x, R):
        if x._free is None:
            x._free = _free_rel(x)
        return x._free
    if isinstance(x, E):
        return _free_expr(x)
    if isinstance(x, (Agg, Win)):
        return max([_free_expr(a) for a in _sub_exprs(x)] + [0])
    if isinstance(x, tuple):
        return max([free_depth(y) for y in x] + [0])
    return 0


def _sub_exprs(x):
    if isinstance(x, Agg):
        return list(x.args) + ([x.filt] if x.filt is not None else [])
    return list(x.args) + list(x.partition) + [o[0] for o in x.order]


def _free_expr(e: E):
    if e.op == "outer":
        return e.args[0]
    if e.op == "sub":
        # args: (kind, rel, lhs exprs, cmp op, quantifier); rel's free depth d means d levels out from inside the
        # subquery's top operator, i.e. d-1 levels out from the row the subquery expression is evaluated on.
        _, rel, lhs, _, _ = e.args
        return max([free_depth(rel) - 1] + [_free_expr(a) for a in lhs] + [0])
    m = 0
    for a in e.args:
        if isinstance(a, E):
            m = max(m, _free_expr(a))
        elif isinstance(a, tuple):
            for b in a:
                if isinstance(b, E):
                    m = max(m, _free_expr(b))
                elif isinstance(b, tuple):
                    for c in b:
                        if isinstance(c, E):
                            m = max(m, _free_expr(c))
    return m


def _rel_exprs(r: R):
    """(exprs evaluated on this operator's input row, child relations)."""
    op, a = r.op, r.args
    if op == "scan":
        return [], []
    if op == "values":
        return [e for row in a[0] for e in row], []
    if op == "filter":
        return [a[1]], [a[0]]
    if op == "project":
        return list(a[1]), [a[0]]
    if op == "join":
        return [a[3]], [a[1], a[2]]
    if op == "agg":
        return list(a[1]) + [y for g in a[2] for y in _sub_exprs(g)], [a[0]]
    if op == "window":
        return [y for w in a[1] for y in _sub_exprs(w)], [a[0]]
    if op == "distinct":
        return [], [a[0]]
    if op == "distinct_on":
        return [k[0] for k in a[2]], [a[0]]
    if op == "setop":
        return [], [a[2], a[3]]
    if op == "sort":
        return [k[0] for k in a[1]], [a[0]]
    raise ValueError(op)


def _free_rel(r: R):
    exprs, kids = _rel_exprs(r)
    m = max([_free_expr(e) for e in exprs] + [0])
    lat = r.op == "join" and r.args[0].startswith("lat_")
    for i, k in enumerate(kids):
        # the right input of a lateral join is evaluated inside the left row's environment
        m = max(m, free_depth(k) - (1 if lat and i == 1 else 0))
    return m


def walk_rel(r: R):
    """All relations reachable from r, including those inside subquery expressions (pre-order, with repeats)."""
    yield r
    exprs, kids = _rel_exprs(r)
    for e in exprs:
        yield from _walk_expr_rels(e)
    for k in kids:
        yield from walk_rel(k)


def _walk_expr_rels(e):
    if not isinstance(e, E):
        return
    if e.op == "sub":
        yield from walk_rel(e.args[1])
        for a in e.args[2]:
            yield from _walk_expr_rels(a)
        return
    for a in e.args:
        if isinstance(a, E):
            yield from _walk_expr_rels(a)
        elif isinstance(a, tuple):
            for b in a:
                if isinstance(b, E):
                    yield from _walk_expr_rels(b)
                elif isinstance(b, tuple):
                    for c in b:
                        if isinstance(c, E):
                            yield from _walk_expr_rels(c)


def walk_exprs(r: R):
    """All scalar expressions of all operators reachable from r (pre-order)."""
    for x in walk_rel(r):
        exprs, _ = _rel_exprs(x)
        for e in exprs:
            yield from _walk_e(e)


def _walk_e(e):
    if not isinstance(e, E):
        return
    yield e
    for a in e.args:
        if isinstance(a, E):
            yield from _walk_e(a)
        elif isinstance(a, tuple):
            for b in a:
                if isinstance(b, E):
                    yield from _walk_e(b)
                elif isinstance(b, tuple):
                    for c in b:
                        if isinstance(c, E):
                            yield from _walk_e(c)


def size(r: R):
    return sum(1 for _ in walk_rel(r))
