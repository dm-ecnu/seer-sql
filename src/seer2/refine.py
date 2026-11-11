"""SEER v2 core loop: partition refinement of a query batch over a sequence of shared, constraint-valid instances.

State: a partition P of the batch (ERROR queries in their own class, never counted as split). Each round
  1. targets: for every class with >= 2 members, the syntactic differences between its members (diff.py) give
     data-shape targets; tier-1 fallback targets when tier 0 is exhausted;
  2. M candidate instances: realised targets (generate.realize) on an empty or the current instance, plus
     constraint-preserving mutations of the current instance;
  3. every candidate is loaded once into the backend (constraints declared in DDL) and every query of every class
     with >= 2 members runs on it;
  4. greedy choice: the candidate that splits most still-together pairs (ties: output novelty) refines P; then the
     next best with positive gain on the refined P, and so on;
Stop: every class is a singleton (all-split); or the Laplace estimate 1/(k+2) < delta over k consecutive executed
instances without refinement AND every tier-0 target of an unsplit pair is covered / UNSAT / given up (saturated); or
the wall budget (budget).

Output signature of a query on an instance: bag of normalised rows; for a query with an outermost ORDER BY whose order
is determinate on that instance (same list under ascending and descending positional tie-breaks) also the list. Two
queries are separated by order only if both are ordered and determinate. A LIMIT query whose tie-broken variants
differ as bags is nondeterministic on that instance and its class is not refined there. Before a split is accepted,
blocks whose outputs agree up to float rounding (rel 1e-6) are merged. Every split is re-executed on a fresh
connection at the end (runner assertion).
"""
from __future__ import annotations

import copy
import datetime as dt
import decimal
import hashlib
import itertools
import math
import random
import time

import sqlglot
from sqlglot import exp

from .aggwit import AggWitness
from .backends import EngineRejected, QueryError, make_backend, transpile
from .diff import Target, fallback, features, pair_targets, unary_atoms
from .generate import AtomEval, Mutator, Witness, _empty, realize
from .pools import Pools
from .query import QInfo
from .schema import Schema

MAX_ROWS = 40
STAGNANT_RESTART = 6
MAX_FAILS = 2
STAR_CLASS = 24                  # classes above this size motivate targets by representative-member pairs only
REPEAT = {"AGG": 3, "DUP": 2}    # randomised realisations: a target counts as covered after this many instances


# ---------------------------------------------------------------------- output signatures
def norm(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (float, decimal.Decimal)):
        f = float(v)
        if f != f:
            return "NaN"
        return float(f"{f:.9g}")
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, str):
        return v.rstrip()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


def _rkey(r):
    return tuple((x is None, type(x).__name__ if not isinstance(x, (int, float)) else "num", x if x is not None else 0)
                 for x in r)


def _h(x):
    return hashlib.md5(repr(x).encode()).hexdigest()[:16]


class Out:
    __slots__ = ("kind", "bag", "lst", "rows", "ordered", "msg")

    def __init__(self, kind, bag=None, lst=None, rows=None, ordered=False, msg=None):
        self.kind, self.bag, self.lst, self.rows, self.ordered, self.msg = kind, bag, lst, rows, ordered, msg

    def sig(self):
        return (self.kind, self.bag, self.lst)


class Prepared:
    """A query transpiled to the backend dialect, with positional tie-break variants for an outer ORDER BY."""

    def __init__(self, sql, read, write):
        self.raw = sql
        try:
            self.sql = transpile(sql, read, write)
        except Exception:
            self.sql = sql
        self.write = write
        self.ordered, self.limited = False, False
        try:
            tree = sqlglot.parse_one(self.sql, read=write)
            self.ordered = tree.args.get("order") is not None
            self.limited = tree.args.get("limit") is not None or tree.args.get("offset") is not None
            self.tree = tree
        except Exception:
            self.tree = None
        self._var = {}

    def variants(self, ncols):
        if ncols not in self._var:
            out = []
            for desc in (False, True):
                t = self.tree.copy()
                o = t.args["order"]
                # the two variants are exact reverses on the output columns, NULLs included (ASC NULLS FIRST vs
                # DESC NULLS LAST); left implicit, sqlglot renders SQLite's ASC as NULLS LAST, and a tie between a
                # NULL and a non-NULL output row then resolves the same way in both variants and goes unnoticed
                for i in range(1, ncols + 1):
                    o.append("expressions", exp.Ordered(this=exp.Literal.number(i), desc=desc, nulls_first=not desc))
                out.append(t.sql(dialect=self.write))
            self._var[ncols] = out
        return self._var[ncols]


def execute(inst, p: Prepared):
    rows, ncols = inst.run(p.sql)
    if isinstance(rows, QueryError):
        return Out("err", msg=rows.msg)
    rows = [tuple(norm(v) for v in r) for r in rows]
    if not p.ordered or p.tree is None:
        srt = sorted(rows, key=_rkey)
        return Out("ok", _h(srt), None, srt)
    va, vd = p.variants(ncols)
    ra, _ = inst.run(va)
    rd, _ = inst.run(vd)
    if isinstance(ra, QueryError) or isinstance(rd, QueryError):
        if p.limited:
            return Out("nondet", msg="tie-break variant failed")
        srt = sorted(rows, key=_rkey)
        return Out("ok", _h(srt), None, srt, ordered=True)
    ra = [tuple(norm(v) for v in r) for r in ra]
    rd = [tuple(norm(v) for v in r) for r in rd]
    sa, sd = sorted(ra, key=_rkey), sorted(rd, key=_rkey)
    if ra == rd:
        return Out("ok", _h(sa), _h(ra), ra, ordered=True)
    if p.limited and sa != sd:
        # which rows survive the LIMIT depends on the tie-break, how many do not (kept as ("#n", count))
        # the plain run's rows (what any replay of this instance in this load order returns) are kept as well
        same_n = len(ra) == len(rd) == len(rows)
        srt = sorted(rows, key=_rkey)
        return Out("nondet", bag=("#n", len(ra)) if same_n else None, lst=_h(srt), rows=srt,
                   msg="tie at LIMIT boundary")
    return Out("ok", _h(sa), None, sa, ordered=True)


def _close(a, b):
    if isinstance(a, float) or isinstance(b, float):
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return False
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))
    return a == b


def approx_same(ra, rb):
    if len(ra) != len(rb):
        return False
    return all(len(x) == len(y) and all(_close(u, v) for u, v in zip(x, y)) for x, y in zip(ra, rb))


def split_class(cls, outs):
    """Blocks of class cls on one instance (list of frozensets); [cls] if it cannot be refined there."""
    if any(outs[q].kind != "ok" for q in cls):
        return [cls]
    groups = {}
    for q in sorted(cls):
        groups.setdefault(outs[q].bag, []).append(q)
    gl = list(groups.values())
    merged = []
    for g in gl:                                       # float-rounding guard
        for m in merged:
            if approx_same(outs[m[0]].rows if not outs[m[0]].ordered else sorted(outs[m[0]].rows, key=_rkey),
                           outs[g[0]].rows if not outs[g[0]].ordered else sorted(outs[g[0]].rows, key=_rkey)):
                m.extend(g)
                break
        else:
            merged.append(list(g))
    blocks = []
    for g in merged:
        if len(g) > 1 and all(outs[q].ordered and outs[q].lst is not None for q in g) \
                and len({outs[q].bag for q in g}) == 1:
            sub = {}
            for q in g:
                sub.setdefault(outs[q].lst, []).append(q)
            blocks += [frozenset(x) for x in sub.values()]
        else:
            blocks.append(frozenset(g))
    return blocks


def npairs(n):
    return n * (n - 1) // 2


# ---------------------------------------------------------------------- the refiner
class Refiner:
    def __init__(self, schema: Schema, queries, backend="duckdb", dialect="duckdb", budget=60.0, seed=0, M=4,
                 delta=0.05, reference=None, source=None, qtimeout=10.0, trace=None, on_db=None):
        """queries: list of (id, sql). reference: id of the reference query (optional)."""
        self.t0 = time.perf_counter()
        self.S, self.budget, self.seed, self.M, self.delta = schema, budget, seed, M, delta
        self.ids = [q for q, _ in queries]
        self.sqls = [s for _, s in queries]
        self.reference = reference
        self.rng = random.Random(f"seer2-{seed}")
        self.backend = make_backend(backend, schema, qtimeout)
        self.prep = [Prepared(s, dialect, self.backend.dialect) for s in self.sqls]
        self.qi = [QInfo(s, schema, dialect) for s in self.sqls]
        self.pools = Pools(schema, self.sqls, dialect, source=source, rng=random.Random(f"pool-{seed}"))
        self.wit = Witness(schema, self.pools, AtomEval(backend, dialect), seed)
        self.mut = Mutator(schema, self.pools, self.rng)
        self.wit.agg = AggWitness(schema, self.pools, self.wit, self.mut, seed)
        self.feats = [features(self.qi[i], i, schema) for i in range(len(self.sqls))]
        self.ua = [unary_atoms(self.qi[i], schema) for i in range(len(self.sqls))]
        self.fb = [fallback(self.qi[i], i, schema) for i in range(len(self.sqls))]
        self.trace = trace
        self.on_db = on_db           # called with (index, db) as soon as an instance refines the partition
        self.tstatus = {}            # target key -> covered | unsat | gaveup | merged | fails:int
        self.ncov = {}               # target key -> instances realised
        self.sem_cov = {}            # query-independent target identity -> instances realised
        self.kind_tries, self.kind_hits = {}, {}
        self.n_exec = 0
        self.dbs = {}                # certificate index -> instance
        self.events = []             # (db index, parent class, blocks)
        self.rounds = []
        self.seen = set()
        self.errors = {}
        self.ndb_errors = {i: 0 for i in range(len(self.sqls))}

    def elapsed(self):
        return time.perf_counter() - self.t0

    def log(self, *a):
        if self.trace:
            self.trace(" ".join(str(x) for x in a))

    # ------------------------------------------------------------------ execution
    def run_all(self, db, qs):
        inst = self.backend.open(db)       # EngineRejected propagates: a generator bug, never swallowed
        try:
            return {q: execute(inst, self.prep[q]) for q in qs}
        finally:
            inst.close()

    def live(self):
        return sorted(q for c in self.P if len(c) > 1 for q in c)

    def gain(self, outs):
        g, blocks = 0, {}
        for c in self.P:
            if len(c) < 2:
                continue
            b = split_class(c, outs)
            if len(b) > 1:
                blocks[c] = b
                g += npairs(len(c)) - sum(npairs(len(x)) for x in b)
        return g, blocks

    # ------------------------------------------------------------------ targets
    def unsplit_pairs(self):
        """Pairs whose differences motivate targets: all pairs of a class, or, for a class larger than STAR_CLASS,
        the star of its representative (the reference if it is in the class, else the first member). The star is
        linear in the class size and still motivates a split of every member; after a split each block has its own
        representative, so pairs between non-representatives are reached in later rounds."""
        ref = self.ids.index(self.reference) if self.reference in self.ids else None
        out = []
        for c in self.P:
            if len(c) < 2:
                continue
            m = sorted(c)
            if len(m) <= STAR_CLASS:
                out += itertools.combinations(m, 2)
            else:
                r = ref if ref in c else m[0]
                out += [(min(r, q), max(r, q)) for q in m if q != r]
        return out

    def targets(self):
        """{target key: (Target, set of unsplit pairs motivating it)} for tier 0, and the tier-1 list."""
        t0 = {}
        for a, b in self.unsplit_pairs():
            ts = pair_targets(self.feats[a], self.feats[b], a, b, self.ua[a], self.ua[b])
            for t in ts:
                k = t.key
                if k in t0:
                    t0[k][1].add((a, b))
                else:
                    t0[k] = (t, {(a, b)})
        t1 = []
        for q in self.live():
            t1 += [t for t in self.fb[q] if t.key not in t0]
        return t0, t1

    def resolved(self, k):
        st = self.tstatus.get(k)
        if st == "covered":
            return self.ncov.get(k, 0) >= REPEAT.get(k[0], 1)
        return st in ("unsat", "gaveup", "merged")

    def sem(self, t):
        """Query-independent identity of a target: occurrences replaced by their tables. Targets with the same
        identity on different members (the same atom forced in two rewrites, a duplicate lineitem row in two
        rewrites) build near-identical instances, so one representative is realised per identity."""
        qi = self.qi[t.q]

        def m(x):
            if isinstance(x, str) and x[:1] == "o" and x[1:].isdigit() and qi.ok and int(x[1:]) < len(qi.occ):
                return qi.occ_by_id(x).table
            if isinstance(x, tuple):
                return tuple(m(y) for y in x)
            return x
        if t.kind == "FORMULA":
            return (t.kind, t.q, t.args)
        return (t.kind, m(t.args))

    def score(self, kind, npairs_):
        """Expected new splits of a target: the unsplit pairs whose difference it addresses, times the (Laplace)
        rate at which realised targets of its kind have refined the partition so far in this batch."""
        tr, sc = self.kind_tries.get(kind, 0), self.kind_hits.get(kind, 0)
        return npairs_ * (sc + 1) / (tr + 2)

    def pick_targets(self, n):
        t0, t1 = self.targets()
        groups = {}
        for t, prs in t0.values():
            if self.resolved(t.key):
                continue
            g = groups.setdefault(self.sem(t), [set(), []])
            g[0] |= prs
            g[1].append((t, prs))
        # a group whose representative has been realised often enough is resolved for all its members
        for sk, (prs, mem) in list(groups.items()):
            if self.sem_cov.get(sk, 0) >= REPEAT.get(sk[0], 1):
                for t, _ in mem:
                    self.tstatus[t.key] = "merged"
                del groups[sk]
        ranked = sorted(groups.items(), key=lambda kv: (min(self._fails(t.key) for t, _ in kv[1][1]),
                                                         -self.score(kv[0][0], len(kv[1][0])), self.rng.random()))
        out = []
        for sk, (prs, mem) in ranked[:n]:
            mem.sort(key=lambda x: (self._fails(x[0].key), -len(x[1]), self.rng.random()))
            out.append(mem[0][0])
        if len(out) < n:
            open1 = [t for t in t1 if not self.resolved(t.key)]
            self.rng.shuffle(open1)
            open1.sort(key=lambda t: (self._fails(t.key), -self.score(t.kind, 1)))
            out += open1[:n - len(out)]
        return out, len(groups)

    def _fails(self, k):
        st = self.tstatus.get(k)
        return st if isinstance(st, int) else 0

    # ------------------------------------------------------------------ candidates
    def candidates(self, cur):
        cands = []
        n_t = max(1, self.M - 1)
        picked, n_open0 = self.pick_targets(n_t)
        if self.trace:
            t0, _ = self.targets()
            self.log(f"  unsplit pairs {len(self.unsplit_pairs())}, tier-0 targets {len(t0)} "
                     f"(open identities {n_open0}); this round: " + "; ".join(f"{t.kind}{t.args}@{self.ids[t.q]}" for t in picked))
        for t in picked:
            base = _empty(self.S) if (t.kind == "EMPTY" or self.rng.random() < 0.5) else cur
            st, db = realize(t, self.qi[t.q], base, self.wit, self.mut)
            self.kind_tries[t.kind] = self.kind_tries.get(t.kind, 0) + 1
            if st == "sat":
                self.tstatus[t.key] = "covered"
                self.ncov[t.key] = self.ncov.get(t.key, 0) + 1
                sk = self.sem(t)
                self.sem_cov[sk] = self.sem_cov.get(sk, 0) + 1
                cands.append((f"{t.kind}{t.args}@{self.ids[t.q]}", db, t.kind))
            elif st == "unsat":
                self.tstatus[t.key] = "unsat"
            else:
                f = self._fails(t.key) + 1
                self.tstatus[t.key] = "gaveup" if f >= MAX_FAILS else f
        while len(cands) < self.M:
            db = copy.deepcopy(cur)
            ops = []
            for _ in range(self.rng.choice([1, 1, 2, 3])):
                if self.rng.random() < 0.35:
                    q = self.rng.choice(self.live() or list(range(len(self.sqls))))
                    pol = {f.fid: (self.rng.random() < 0.8) for f in self.qi[q].formulas} if self.qi[q].ok else None
                    st, nd, _ = self.wit.solve(self.qi[q], db, pol=pol)
                    if st == "sat":
                        db = nd
                        ops.append(f"witness@{self.ids[q]}")
                else:
                    name, ok = self.mut.random_op(db)
                    if ok:
                        ops.append(name)
                self.S.check(db)                     # validity after every change
            cands.append(("mutate:" + "+".join(ops or ["none"]), db, "mutate"))
        return cands, n_open0

    # ------------------------------------------------------------------ main loop
    def run(self):
        S = self.S
        empty = _empty(S)
        allq = list(range(len(self.sqls)))
        outs0 = self.run_all(empty, allq)
        self.n_exec += 1
        for q in allq:
            if outs0[q].kind == "err":
                self.errors[q] = outs0[q].msg
        self.P = [frozenset(q for q in allq if q not in self.errors)]
        self.P = [c for c in self.P if c]
        self.log(f"[init] {len(allq)} queries, ERROR: {sorted(self.errors)}; executing the empty instance")
        self._apply([("empty", empty, outs0)], 0)
        cur = empty
        k_stall = 0
        stagnant = 0
        rnd = 0
        why = "budget"
        while True:
            if all(len(c) == 1 for c in self.P):
                why = "all-split"
                break
            if self.elapsed() > self.budget:
                why = "budget"
                break
            rnd += 1
            cands, n_open0 = self.candidates(cur)
            live = self.live()
            ex = []
            kinds = {}
            for name, db, kind in cands:
                S.check(db)
                outs = self.run_all(db, live)
                self.n_exec += 1
                for q in live:
                    if outs[q].kind == "err":
                        self.ndb_errors[q] += 1
                ex.append((name, db, outs))
                kinds[name] = kind
            before = sum(npairs(len(c)) for c in self.P)
            chosen = self._apply(ex, rnd)
            for nm in self._last_chosen:
                k = kinds.get(nm)
                if k:
                    self.kind_hits[k] = self.kind_hits.get(k, 0) + 1
            after = sum(npairs(len(c)) for c in self.P)
            if chosen:
                k_stall = 0
                stagnant = 0
                cur = chosen[-1][1]
            else:
                k_stall += len(ex)
                stagnant += 1
                best = max(ex, key=lambda x: self._novelty(x[2]))
                if self.rng.random() < 0.5:
                    cur = best[1]
            for _, _, outs in ex:
                for q, o in outs.items():
                    self.seen.add((q, o.sig()))
            if sum(len(v) for v in cur.values()) > MAX_ROWS or stagnant > STAGNANT_RESTART:
                cur, stagnant = _empty(S), 0
            self.rounds.append({"round": rnd, "executed": len(ex), "chosen": [c[0] for c in chosen],
                                "pairs_split": before - after, "classes": len(self.P),
                                "open_tier0": n_open0, "t": round(self.elapsed(), 3)})
            if 1.0 / (k_stall + 2) < self.delta:
                t0, _ = self.targets()
                if all(self.resolved(k) for k in t0):
                    why = "saturated"
                    break
        self.verify()
        return self.result(why)

    def _novelty(self, outs):
        return sum(1 for q, o in outs.items() if (q, o.sig()) not in self.seen)

    def _apply(self, ex, rnd):
        """Greedy: repeatedly take the executed candidate with the largest gain on the current partition."""
        chosen = []
        self._last_chosen = []
        pool = list(ex)
        if self.trace:
            for name, db, outs in pool:
                g, _ = self.gain(outs)
                rows = {t: len(v) for t, v in db.items() if v}
                self.log(f"[round {rnd}]   candidate {name}: rows {rows}, would split {g} pair(s)")
        while pool:
            scored = []
            for i, (name, db, outs) in enumerate(pool):
                g, blocks = self.gain(outs)
                scored.append((g, self._novelty(outs), -i, i, blocks))
            g, nov, _, i, blocks = max(scored, key=lambda x: x[:3])
            if g <= 0:
                break
            name, db, outs = pool.pop(i)
            idx = len(self.dbs)
            self.dbs[idx] = db
            newP = []
            for c in self.P:
                if c in blocks:
                    self.events.append({"db": idx, "parent": sorted(c), "blocks": [sorted(b) for b in blocks[c]],
                                        "sigs": {q: outs[q].sig() for q in c}})
                    newP += blocks[c]
                else:
                    newP.append(c)
            self.P = newP
            if self.on_db:
                self.on_db(idx, db)
            chosen.append((idx, db, name, g))
            self._last_chosen.append(name)
            self.log(f"[round {rnd}] chose DB#{idx} ({name}, {sum(len(v) for v in db.values())} rows): "
                     f"split {g} pair(s) -> classes {self.fmt_classes()}")
        return [(c[0], c[1]) for c in chosen]

    def fmt_classes(self):
        return [sorted(self.ids[q] for q in c) for c in sorted(self.P, key=lambda c: min(c))]

    # ------------------------------------------------------------------ certification
    def verify(self):
        """Re-execute every split on a fresh connection; assert constraints and the same blocks."""
        for ev in self.events:
            db = self.dbs[ev["db"]]
            self.S.check(db)
            outs = self.run_all(db, ev["parent"])
            for q in ev["parent"]:
                assert outs[q].sig() == ev["sigs"][q], \
                    f"split on DB#{ev['db']} not reproducible for query {self.ids[q]}"
            blocks = sorted(sorted(b) for b in split_class(frozenset(ev["parent"]), outs))
            assert blocks == sorted(ev["blocks"]), f"split on DB#{ev['db']} not reproducible"

    def pair_certificate(self, a, b):
        for ev in self.events:
            if a in ev["parent"] and b in ev["parent"]:
                ba = next(i for i, x in enumerate(ev["blocks"]) if a in x)
                bb = next(i for i, x in enumerate(ev["blocks"]) if b in x)
                if ba != bb:
                    return ev["db"]
        return None

    def result(self, why):
        def ser(db):
            return {t: [{c: norm(v) for c, v in r.items()} for r in rows] for t, rows in db.items() if rows}
        classes = sorted([sorted(c) for c in self.P], key=min)
        res = {
            "ids": self.ids,
            "classes": [[self.ids[q] for q in c] for c in classes],
            "error": {self.ids[q]: m for q, m in self.errors.items()},
            "stop": why,
            "wall": round(self.elapsed(), 3),
            "n_db": len(self.dbs),
            "n_executed": self.n_exec,
            "rounds": self.rounds,
            "splits": [{"db": e["db"], "parent": [self.ids[q] for q in e["parent"]],
                        "blocks": [[self.ids[q] for q in b] for b in e["blocks"]]} for e in self.events],
            "dbs": {i: ser(db) for i, db in self.dbs.items()},
            "targets": self._tstats(),
            "witness": dict(self.wit.stats),
            "runtime_errors": {self.ids[q]: n for q, n in self.ndb_errors.items() if n},
        }
        if self.reference is not None:
            r = self.ids.index(self.reference)
            ver = {}
            for q in range(len(self.ids)):
                if q == r:
                    continue
                if q in self.errors or r in self.errors:
                    ver[self.ids[q]] = {"verdict": "ERROR"}
                elif any(q in c and r in c for c in self.P):
                    ver[self.ids[q]] = {"verdict": "EQ_BOUNDED" if why != "budget" else "UNSPLIT_BUDGET"}
                else:
                    ver[self.ids[q]] = {"verdict": "NEQ", "db": self.pair_certificate(q, r)}
            res["verdicts"] = ver
        return res

    def _tstats(self):
        t0, _ = self.targets()
        out = {}
        for k, st in self.tstatus.items():
            st = st if isinstance(st, str) else "open"
            out[st] = out.get(st, 0) + 1
        out["tier0_unsplit_open"] = sum(1 for k in t0 if not self.resolved(k))
        return out
