"""Fast tier: random constraint-valid databases from per-column value pools (SQLWitness-style), with repair.

Values come from seer2.pools.Pools (schema seeds + literals of the batch's queries attached to the columns they
are compared with, so predicates flip across instances); FK columns point to existing parent rows (or NULL under
MATCH SIMPLE when nullable); PK/UNIQUE collisions are repaired by minting a fresh key (seer2 Mutator.fresh_copy);
CHECK violations by resampling. Every instance is validated with Schema.violations before use (runner assertion).
Later instances are either fresh or a chain of constraint-preserving mutations (clone with or without children,
cascade delete, update to a tie, set NULL, repoint FK) of an earlier one, which makes duplicates, empty groups,
ties and outer-join padding likely at small sizes.
"""
from __future__ import annotations

import copy
import random

from seer2.generate import Mutator
from seer2.pools import Pools
from seer2.schema import eval_check


class FastGen:
    def __init__(self, schema, sqls, dialect="duckdb", seed=0, max_rows=4):
        self.S = schema
        self.rng = random.Random(seed)
        self.pools = Pools(schema, sqls, dialect=dialect, rng=self.rng)
        self.mut = Mutator(schema, self.pools, self.rng)
        self.max_rows = max_rows
        self.history = []

    def fresh(self):
        S, rng = self.S, self.rng
        for _ in range(20):
            db = {t: [] for t in S.order}
            for t in S.order:
                tb = S.tables[t]
                n = rng.choice([0, 1, 1, 2, 2, 3, self.max_rows])
                fkcols = {c for fk in tb.fks for c in fk.cols}
                for _ in range(n):
                    row = {c.name: rng.choice(self.pools.col[(t, c.name)]) for c in tb.columns}
                    ok = True
                    for fk in tb.fks:
                        parents = [p for p in db.get(fk.ref, []) if all(p[c] is not None for c in fk.ref_cols)]
                        nullable = all(S.column(t, c).nullable for c in fk.cols)
                        if parents and not (nullable and rng.random() < 0.15) and not (fk.deferred and fk.ref == t):
                            p = rng.choice(parents)
                            for fc, pc in zip(fk.cols, fk.ref_cols):
                                row[fc] = p[pc]
                        elif nullable:
                            for fc in fk.cols:
                                row[fc] = None
                        else:
                            ok = False
                    if not ok:
                        continue
                    for ch in tb.checks:
                        tries = 0
                        while eval_check(ch.expr, row) is False and tries < 10:
                            for c in ch.cols:
                                if c not in fkcols:
                                    row[c] = rng.choice(self.pools.col[(t, c)])
                            tries += 1
                        if eval_check(ch.expr, row) is False:
                            ok = False
                    if not ok:
                        continue
                    keys = [tuple(r.get(c) for c in (tb.pk or [])) for r in db[t]] if tb.pk else []
                    dup = tb.pk and tuple(row[c] for c in tb.pk) in keys
                    udup = any(all(row[c] is not None for c in u) and
                               tuple(row[c] for c in u) in {tuple(r[c] for c in u) for r in db[t]}
                               for u in tb.uniques)
                    if dup or udup:
                        new = self.mut.fresh_copy(db, t, row)
                        if new is None:
                            continue
                        row = new
                    db[t].append(row)
            if not S.violations(db):
                return db
        return {t: [] for t in S.order}

    def next(self):
        if self.history and self.rng.random() < 0.5:
            db = copy.deepcopy(self.rng.choice(self.history[-8:]))
            for _ in range(self.rng.randint(1, 3)):
                self.mut.random_op(db)
            if self.S.violations(db):
                db = self.fresh()
        else:
            db = self.fresh()
        assert not self.S.violations(db), self.S.violations(db)[:3]
        self.history.append(db)
        if len(self.history) > 64:
            self.history = self.history[-32:]
        return db
