"""Join blocks: flattening of inner/cross join trees and key-lookup join planning (used by the encoder and by the
size estimate).

A join block is a maximal tree of non-lateral INNER/CROSS joins, optionally with the WHERE filter directly above
it. Its leaves are arbitrary relations; its predicate is the conjunction of every ON condition and the WHERE
predicate. Under bag semantics the block equals the product of the leaves filtered by that conjunction, in any
join order (inner joins and products commute and associate; a row survives iff every conjunct is TRUE).

Key lookup. When a leaf L is a base table (or a filter of one) with a key K (PRIMARY KEY or UNIQUE), and every
column of K is bound by an equality conjunct `L.k = f(row so far)` whose other side only reads leaves already
joined, then for any partial row at most one existing tuple of L satisfies the binding conjuncts: two would share
the non-NULL key value f(...), which Σ forbids. The join step then emits one slot per partial row whose values are
an if-then-else selection over L's k slots, instead of k slots, so a chain of FK->PK joins over t tables has
k slots instead of k^t. The encoding is exact on every Σ-valid database (the key constraints are hard constraints
of the encoding), not an approximation.
"""
from __future__ import annotations

from . import ir


def conjuncts(e):
    if e.op == "fn" and e.args[0] == "and":
        return conjuncts(e.args[1]) + conjuncts(e.args[2])
    if e == ir.TRUE:
        return []
    return [e]


def cols0(e):
    """Column positions of the current row read by e, or None if e contains a subquery (it may read any)."""
    out = set()

    def go(x):
        if x.op == "sub":
            return False
        if x.op == "col":
            out.add(x.args[0])
        for a in x.args:
            if isinstance(a, ir.E):
                if not go(a):
                    return False
            elif isinstance(a, tuple):
                for b in a:
                    if isinstance(b, ir.E):
                        if not go(b):
                            return False
                    elif isinstance(b, tuple):
                        for c in b:
                            if isinstance(c, ir.E) and not go(c):
                                return False
        return True

    return out if go(e) else None


def is_block_join(r):
    return r.op == "join" and r.args[0] in ("inner", "cross")


def flatten(r, off=0, leaves=None, conds=None):
    """leaves: [(rel, offset)]; conds: [(expr, offset, width, positions)] with absolute positions."""
    if leaves is None:
        leaves, conds = [], []
    if is_block_join(r):
        _, L, Rr, cond = r.args
        flatten(L, off, leaves, conds)
        flatten(Rr, off + L.arity, leaves, conds)
        add_cond(conds, cond, off, r.arity)
    else:
        leaves.append((r, off))
    return leaves, conds


def add_cond(conds, e, off, width):
    for c in conjuncts(e):
        cs = cols0(c)
        pos = frozenset(range(off, off + width)) if cs is None else frozenset(off + i for i in cs)
        conds.append((c, off, width, pos))


def leaf_keys(leaf, schema):
    """Key column positions (lists) guaranteed unique among the existing tuples of leaf."""
    if leaf.op == "filter":
        return leaf_keys(leaf.args[0], schema)
    if leaf.op != "scan":
        return []
    tb = schema.tables[leaf.args[0]]
    idx = {c.name: j for j, c in enumerate(tb.columns)}
    keys = ([tb.pk] if tb.pk else []) + list(tb.uniques)
    return [[idx[c] for c in k] for k in keys if k]


def plan(leaves, conds, sizes, keys):
    """Join order: [(leaf index, None | [cond indices binding a key])]. Greedy from every start leaf; lookups first,
    then the smallest leaf connected by a conjunct, else the smallest leaf; keep the start with the fewest slots."""
    n = len(leaves)
    rng = [frozenset(range(off, off + r.arity)) for r, off in leaves]
    best = None
    for s in range(n):
        order, joined, pos, est = [(s, None)], {s}, set(rng[s]), sizes[s]
        while len(joined) < n:
            step = None
            for L in range(n):
                if L in joined:
                    continue
                for key in keys[L]:
                    use = []
                    for kc in key:
                        a = leaves[L][1] + kc
                        ci = _binding(conds, a, rng[L], pos)
                        if ci is None:
                            break
                        use.append(ci)
                    else:
                        step = (L, sorted(set(use)))
                        break
                if step:
                    break
            if step is None:
                conn = [L for L in range(n) if L not in joined
                        and any(p & rng[L] and p - rng[L] and p - rng[L] <= pos for _, _, _, p in conds)]
                pool = conn or [L for L in range(n) if L not in joined]
                L = min(pool, key=lambda x: (sizes[x], x))
                step = (L, None)
                est *= max(sizes[L], 1)
            order.append(step)
            joined.add(step[0])
            pos |= rng[step[0]]
        if best is None or est < best[0]:
            best = (est, order)
    return best[1], best[0]


def _binding(conds, a, lrange, pos):
    """Index of a conjunct `col(a) = f(...)` with f reading only positions in pos (none of lrange)."""
    for i, (c, off, _, p) in enumerate(conds):
        if c.op != "fn" or c.args[0] != "eq":
            continue
        for x, y in ((c.args[1], c.args[2]), (c.args[2], c.args[1])):
            if x.op == "col" and off + x.args[0] == a:
                ys = cols0(y)
                if ys is None:
                    continue
                ys = {off + j for j in ys}
                if not (ys & lrange) and ys <= pos:
                    return i
    return None
