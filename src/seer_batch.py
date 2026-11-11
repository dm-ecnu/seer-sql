"""SEER-batch prototype: constraint-respecting batch refutation of LLM rewrites.

Input : one original query Q0, a batch of candidate rewrites C1..Cn, the TPC-H DDL (PK / FK incl. composite FK /
        NOT NULL; deletes cascade to dependants).
Output: per candidate REFUTED (with a concrete small database that satisfies every declared constraint and on which
        DuckDB returns different results for Q0 and Ci; re-executed on a fresh connection before it is reported),
        EQ_BOUNDED (search terminated by its saturation rule without a counterexample: "consistent with Q0 on k
        constraint-satisfying databases"; evidence, not a proof), TIMEOUT (batch budget exhausted first) or FAIL
        (candidate raises an engine error on every database).

Search (one batch = one original + all its rewrites, one shared state):
  * the batch keeps a single current database D (always constraint-satisfying; asserted after every change) and the
    partition of {Q0} + live candidates by output on every database executed so far; each executed database is run
    once for the whole batch, and only candidates still in Q0's class are executed (incremental);
  * mutation operators that preserve PK/FK/NOT NULL by construction: SMT witness insertion (below), clone a row (and
    optionally its dependants) under fresh keys, cascade delete, non-key update (pool value or copy of another row's
    value -> ties/duplicates), FK re-pointing;
  * SMT witness insertion (Z3): for a query of the batch, one fresh row per table occurrence (plus FK parents), with
    the WHERE/ON boolean structure encoded over atom variables. Atoms over non-key columns are given by truth tables
    over finite literal-derived domains that DuckDB itself evaluates (no SQL expression is re-implemented); key-key
    equalities are Z3 integer equalities; PK uniqueness and (composite) FK membership against the current database
    are hard constraints. Uncovered (atom, polarity) targets of all batch queries are forced one at a time.
  * acceptance: among M mutants keep the one that maximises the number of output classes in Q0's class (ties: output
    novelty); restart from an empty database when the size cap is reached or the search stagnates.
  * termination: all candidates refuted; or saturation = every atom target covered or proven infeasible, at least
    MIN_ROUNDS rounds, and STALL rounds since the last refutation; or the batch budget (BUDGET_S, wall clock for the
    whole batch, i.e. the same 120 s ParSEval gets for ONE pair).

Usage: .venv/bin/python src/seer_batch.py data/rewrites.jsonl runs/seer_s0.jsonl --seed 0 --workers 10
"""
import argparse, copy, datetime as dt, decimal, hashlib, json, os, random, re, sys, time
import subprocess
from concurrent.futures import ThreadPoolExecutor

import duckdb, pyarrow as pa, sqlglot, z3
from sqlglot import exp

sys.path.insert(0, os.path.dirname(__file__))
from bounded import (SCHEMA, PK, FK, ORDER, KEYCOLS, QUERIES, PA_T, SF, literals, tiebreak, run, same, norm,  # noqa
                     check_constraints, to_arrow, I, N, S, D)
import pyarrow.parquet as pq

BUDGET_S = 120.0          # per batch (ParSEval: 2 x GEN_LIMIT_S=60 per pair, runs/run_parseval.py)
MIN_ROUNDS = 60
STALL = 120
MUTANTS = 4
MAX_ROWS = 48
HARD_EXTRA_S = 180         # per-batch process kill = budget + this (SF0.01 load + final verification)
QTIMEOUT = 10             # per query execution on synthetic DBs (same as bounded.py)
SF_TIMEOUT = 60
COLTAB = {c: t for t, cols in SCHEMA.items() for c, _ in cols}
COLTY = {c: ty for t, cols in SCHEMA.items() for c, ty in cols}
PARENTS = {t: [(fc, p) for fc, p in FK.get(t, [])] for t in ORDER}
CHILDREN = {t: [(c, fc) for c in ORDER for fc, p in FK.get(c, []) if p == t] for t in ORDER}
CMP = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.Like, exp.ILike, exp.Is)


# ------------------------------------------------------------------------------------------- value pools
def build_pools(sqls, rng):
    strs, nums, dates = literals(sqls)
    sf = {t: pq.read_table(f"{SF}/{t}.parquet").to_pylist() for t in ORDER}
    pools = {}
    for t, cols in SCHEMA.items():
        sample = rng.sample(sf[t], min(len(sf[t]), 200))
        for c, ty in cols:
            if c in KEYCOLS:
                continue
            vals = list(dict.fromkeys(r[c] for r in sample))[:6]
            if ty == S:
                vals += sorted(strs)
            elif ty == D:
                vals += sorted(dates)
            elif ty == I:
                vals += sorted({int(x) for x in nums if x == int(x) and abs(x) < 10 ** 9})
            elif ty == N:
                vals += sorted({x.quantize(decimal.Decimal("0.01")) for x in nums if abs(x) < 10 ** 12})
            vals = list(dict.fromkeys(vals))
            if len(vals) > 60:
                vals = vals[:6] + rng.sample(vals[6:], 54)
            pools[c] = vals
    return pools


def pool_table(vals, ty, name="x0", idx="i0"):
    return pa.table({idx: pa.array(range(len(vals)), pa.int64()), name: pa.array(vals, PA_T[ty])})


# ------------------------------------------------------------------------------------------- query analysis
class QInfo:
    """Scopes, table occurrences, and WHERE/ON formulas of one query, with atoms resolved to occurrences."""

    def __init__(self, sql):
        self.ok = True
        self.occ = []          # (occ_id, table)
        self.formulas = []     # (scope_id, sqlglot condition)
        self.extra = []        # CASE/IF conditions etc.: atoms only, no structural requirement
        try:
            self.tree = sqlglot.parse_one(sql, read="duckdb")
        except Exception:
            self.ok = False
            return
        self.scope_occ = {}    # id(select) -> {alias: occ_id}
        self.parent = {}
        sels = list(self.tree.find_all(exp.Select))
        for s in sels:
            p = s.parent
            while p is not None and not isinstance(p, exp.Select):
                p = p.parent
            self.parent[id(s)] = id(p) if p is not None else None
            m = {}
            srcs = []
            fr = s.args.get("from") or s.args.get("from_")
            if fr is not None:
                srcs.append(fr.this)
            for j in s.args.get("joins") or []:
                srcs.append(j.this)
            for src in srcs:
                if isinstance(src, exp.Table) and src.name.lower() in SCHEMA:
                    t = src.name.lower()
                    alias = (src.alias or t).lower()
                    oid = f"o{len(self.occ)}"
                    self.occ.append((oid, t))
                    m[alias] = oid
            self.scope_occ[id(s)] = m
        for s in sels:
            w = s.args.get("where")
            if w is not None:
                self.formulas.append((id(s), w.this))
            for j in s.args.get("joins") or []:
                on = j.args.get("on")
                if on is not None:
                    self.formulas.append((id(s), on))
            for node in s.expressions:
                for c in node.find_all(exp.If):
                    self.extra.append((id(s), c.this))
                for c in node.find_all(exp.Case):
                    for ifs in c.args.get("ifs") or []:
                        self.extra.append((id(s), ifs.this))
            h = s.args.get("having")
            if h is not None:
                self.extra.append((id(s), h.this))

    def scope_of(self, node):
        p = node.parent
        while p is not None and not isinstance(p, exp.Select):
            p = p.parent
        return id(p) if p is not None else None

    def resolve(self, col, sid):
        """Column -> (occ_id, column) or None (column of a CTE / derived table / alias)."""
        name = col.name.lower()
        if name not in COLTAB:
            return None
        q = col.table.lower() if col.table else None
        t = COLTAB[name]
        while sid is not None:
            m = self.scope_occ.get(sid, {})
            if q is not None:
                if q in m and dict(self.occ)[m[q]] == t:
                    return m[q], name
            else:
                for alias, oid in m.items():
                    if dict(self.occ)[oid] == t:
                        return oid, name
            sid = self.parent.get(sid)
        return None


# ------------------------------------------------------------------------------------------- atoms
class Atoms:
    """Global atom registry for a batch. An atom is a predicate without subqueries/aggregates over 1-2 base columns.
    kind 'u': one non-key column, truth set over its pool; 'b': two non-key columns, allowed index pairs;
    'k': EQ/NEQ between two key columns (Z3 integer (dis)equality)."""

    def __init__(self, pools, con):
        self.pools, self.con = pools, con
        self.cache = {}

    def get(self, node, cols):
        """cols: list of base column names in order of appearance (distinct refs). Returns atom dict or None."""
        if node.find(exp.Subquery) or node.find(exp.Select) or node.find(exp.AggFunc):
            return None
        if isinstance(node, exp.Is):
            return None
        names = [c for c in cols]
        if len(names) == 0 or len(names) > 2:
            return None
        if any(n in KEYCOLS for n in names):
            if len(names) == 2 and all(n in KEYCOLS for n in names) and isinstance(node, (exp.EQ, exp.NEQ)) \
                    and all(isinstance(x, exp.Column) for x in (node.this, node.expression)):
                return {"kind": "k", "eq": isinstance(node, exp.EQ)}
            return None
        e = node.copy()
        refs = list(e.find_all(exp.Column))
        order = []
        for r in refs:
            key = (r.table.lower() if r.table else "", r.name.lower())
            if key not in order:
                order.append(key)
        if len(order) != len(names):
            return None
        for r in refs:
            key = (r.table.lower() if r.table else "", r.name.lower())
            r.replace(exp.column(f"x{order.index(key)}"))
        sql = e.sql(dialect="duckdb")
        ck = (sql, tuple(names))
        if ck in self.cache:
            return self.cache[ck]
        try:
            if len(names) == 1:
                self.con.register("p0", pool_table(self.pools[names[0]], COLTY[names[0]]))
                truth = {r[0] for r in self.con.execute(f"SELECT i0 FROM p0 WHERE ({sql})").fetchall()}
                at = {"kind": "u", "true": truth, "n": len(self.pools[names[0]]), "sql": sql}
            else:
                self.con.register("p0", pool_table(self.pools[names[0]], COLTY[names[0]]))
                self.con.register("p1", pool_table(self.pools[names[1]], COLTY[names[1]], "x1", "i1"))
                truth = set(self.con.execute(f"SELECT i0, i1 FROM p0, p1 WHERE ({sql})").fetchall())
                at = {"kind": "b", "true": truth, "sql": sql}
        except Exception:
            at = None
        self.cache[ck] = at
        return at


# ------------------------------------------------------------------------------------------- SMT witness
class Witness:
    def __init__(self, pools, atoms, rng):
        self.pools, self.atoms, self.rng = pools, atoms, rng

    def encode(self, qi, db, force=None, scope_pol=None, mode="mcdc"):
        """Build and solve the witness problem for QInfo qi on top of db. force: (atom_key, polarity).
        mode for a forced atom: 'mcdc' = the formula containing it must flip with it (MC/DC-style: the other
        conditions are chosen so that this atom decides the outcome), all other formulas true; 'loose' = only the
        formulas not containing it must be true; 'bare' = no formula requirement.
        Returns (rows_by_table, realised {atom_key: [bool]}) or None if UNSAT/unknown."""
        s = z3.Solver()
        s.set("timeout", 2000)
        occ = list(qi.occ)
        # FK closure: one optional fresh parent row per (occurrence, FK); it is inserted only if referenced
        hidden = set()
        i = 0
        while i < len(occ):
            oid, t = occ[i]
            for fcols, p in PARENTS[t]:
                if len(occ) < 24:
                    h = f"h{len(occ)}"
                    occ.append((h, p)); hidden.add(h)
            i += 1
        V = {}
        for oid, t in occ:
            for c, ty in SCHEMA[t]:
                v = z3.Int(f"{oid}.{c}")
                V[(oid, c)] = v
                if c in KEYCOLS:
                    s.add(v >= 1, v <= 10 ** 6)
                else:
                    s.add(v >= 0, v < len(self.pools[c]))
        # PK: fresh w.r.t. existing rows; two occurrences of one table with equal PK are the same row
        for oid, t in occ:
            pk = PK[t]
            for r in db[t]:
                s.add(z3.Or([V[(oid, c)] != r[c] for c in pk]))
        for i, (o1, t1) in enumerate(occ):
            for o2, t2 in occ[i + 1:]:
                if t1 == t2:
                    s.add(z3.Implies(z3.And([V[(o1, c)] == V[(o2, c)] for c in PK[t1]]),
                                     z3.And([V[(o1, c)] == V[(o2, c)] for c, _ in SCHEMA[t1]])))
        # FK membership (composite FK as tuple)
        for oid, t in occ:
            for fcols, p in PARENTS[t]:
                pcols = PK[p]
                opts = [z3.And([V[(oid, fc)] == r[pc] for fc, pc in zip(fcols, pcols)]) for r in db[p]]
                opts += [z3.And([V[(oid, fc)] == V[(po, pc)] for fc, pc in zip(fcols, pcols)])
                         for po, pt in occ if pt == p]
                s.add(z3.Or(opts) if opts else z3.BoolVal(False))
        # formulas
        realised = {}
        fresh = [0]

        def enc(node, sid):
            if isinstance(node, exp.Paren):
                return enc(node.this, sid)
            if isinstance(node, exp.And):
                return z3.And(enc(node.this, sid), enc(node.expression, sid))
            if isinstance(node, exp.Or):
                return z3.Or(enc(node.this, sid), enc(node.expression, sid))
            if isinstance(node, exp.Not):
                return z3.Not(enc(node.this, sid))
            if isinstance(node, CMP):
                refs, ok = [], True
                for col in node.find_all(exp.Column):
                    r = qi.resolve(col, qi.scope_of(col))
                    if r is None:
                        ok = False
                        break
                    if r not in refs:
                        refs.append(r)
                if ok and len({c for _, c in refs}) == len(refs):
                    at = self.atoms.get(node, [c for _, c in refs])
                    if at is not None:
                        b = atom_bool(at, refs)
                        key = (at.get("sql", "k"), tuple(c for _, c in refs))
                        realised.setdefault(key, []).append(b)
                        return b
            fresh[0] += 1
            return z3.Bool(f"free{fresh[0]}")

        def atom_bool(at, refs):
            fresh[0] += 1
            a = z3.Bool(f"atom{fresh[0]}")
            s.add(a == atom_expr(at, refs))
            return a

        def atom_expr(at, refs):
            if at["kind"] == "k":
                (o1, c1), (o2, c2) = refs
                return (V[(o1, c1)] == V[(o2, c2)]) if at["eq"] else (V[(o1, c1)] != V[(o2, c2)])
            if at["kind"] == "u":
                (o, c), = refs
                tv = V[(o, c)]
                return z3.Or([tv == i for i in at["true"]]) if at["true"] else z3.BoolVal(False)
            (o0, c0), (o1, c1) = refs
            a, b = V[(o0, c0)], V[(o1, c1)]
            return z3.Or([z3.And(a == i, b == j) for i, j in at["true"]]) if at["true"] else z3.BoolVal(False)

        fbools = []
        for sid, f in qi.formulas:
            fbools.append(enc(f, sid))
        for sid, f in qi.extra:
            enc(f, sid)
        holds = set()
        if force is not None:
            key, fpol = force
            if key not in realised:
                return None
            avars = realised[key]
            s.add(z3.And([a == z3.BoolVal(fpol) for a in avars]))
            if mode != "bare":
                for k, b in enumerate(fbools):
                    hit = [a for a in avars if not z3.eq(z3.simplify(z3.substitute(b, (a, z3.BoolVal(True)))),
                                                          z3.simplify(z3.substitute(b, (a, z3.BoolVal(False)))))]
                    if hit:
                        holds.add(k)
                if mode == "mcdc" and holds:
                    flips = []
                    for k in holds:
                        b = fbools[k]
                        t_ = z3.substitute(b, *[(a, z3.BoolVal(True)) for a in avars])
                        f_ = z3.substitute(b, *[(a, z3.BoolVal(False)) for a in avars])
                        flips.append(z3.Xor(t_, f_))
                    s.add(z3.Or(flips))
        if mode != "bare":
            for k, b in enumerate(fbools):
                if k in holds:
                    continue
                pol = True if scope_pol is None else scope_pol.get(k, None)
                if pol is not None:
                    s.add(b if pol else z3.Not(b))
        if s.check() != z3.sat:
            return None
        m = s.model()
        vals = {}
        for oid, t in occ:
            r = {}
            for c, ty in SCHEMA[t]:
                val = m.eval(V[(oid, c)], model_completion=True).as_long()
                r[c] = val if c in KEYCOLS else self.pools[c][val]
            vals[oid] = (t, r)
        keep = {oid for oid, _ in occ if oid not in hidden}
        changed = True
        while changed:                      # hidden parents referenced by kept rows
            changed = False
            for oid in list(keep):
                t, r = vals[oid]
                for fcols, p in PARENTS[t]:
                    fk = tuple(r[c] for c in fcols)
                    for h in hidden - keep:
                        pt, pr = vals[h]
                        if pt == p and tuple(pr[c] for c in PK[p]) == fk:
                            keep.add(h); changed = True
        rows = {}
        seen = set()
        for oid, t in occ:
            if oid not in keep:
                continue
            r = vals[oid][1]
            k = (t, tuple(r[c] for c in PK[t]))
            if k in seen:
                continue
            seen.add(k)
            rows.setdefault(t, []).append(r)
        got = {key: [z3.is_true(m.eval(b, model_completion=True)) for b in bs] for key, bs in realised.items()}
        return rows, got


# ------------------------------------------------------------------------------------------- DB mutations
def cascade_delete(db, t, row):
    stack = [(t, row)]
    while stack:
        t, row = stack.pop()
        if row not in db[t]:
            continue
        db[t] = [r for r in db[t] if r is not row]
        for child, fcols in CHILDREN[t]:
            pk = tuple(row[c] for c in PK[t])
            for r in list(db[child]):
                if tuple(r[c] for c in fcols) == pk:
                    stack.append((child, r))


def fresh_key(db, t, base_row, rng):
    """Copy of base_row with a PK not present in db[t] (only the non-FK PK component is changed)."""
    keys = {tuple(r[c] for c in PK[t]) for r in db[t]}
    fkcols = {c for fc, _ in PARENTS[t] for c in fc}
    free = [c for c in PK[t] if c not in fkcols]
    new = dict(base_row)
    if free:
        c = free[0]
        used = {r[c] for r in db[t]}
        new[c] = max(used | {0}) + rng.randint(1, 3)
        return new
    # partsupp: PK = (ps_partkey, ps_suppkey), both FKs -> choose an unused (part, supplier) pair
    pairs = [(p["p_partkey"], s_["s_suppkey"]) for p in db["part"] for s_ in db["supplier"]]
    pairs = [x for x in pairs if x not in keys]
    if not pairs:
        return None
    new["ps_partkey"], new["ps_suppkey"] = rng.choice(pairs)
    return new


def m_clone(db, pools, rng):
    ts = [t for t in ORDER if db[t]]
    if not ts:
        return False
    t = rng.choice(ts)
    r = rng.choice(db[t])
    new = fresh_key(db, t, r, rng)
    if new is None:
        return False
    db[t].append(new)
    if rng.random() < 0.5:  # deep clone of direct dependants
        pk_old = tuple(r[c] for c in PK[t]); pk_new = tuple(new[c] for c in PK[t])
        for child, fcols in CHILDREN[t]:
            for cr in [x for x in db[child] if tuple(x[c] for c in fcols) == pk_old]:
                nc = dict(cr)
                for fc, v in zip(fcols, pk_new):
                    nc[fc] = v
                if tuple(nc[c] for c in PK[child]) in {tuple(x[c] for c in PK[child]) for x in db[child]}:
                    nc = fresh_key(db, child, nc, rng)
                    if nc is None:
                        continue
                db[child].append(nc)
    return True


def m_delete(db, pools, rng):
    ts = [t for t in ORDER if db[t]]
    if not ts:
        return False
    t = rng.choice(ts)
    cascade_delete(db, t, rng.choice(db[t]))
    return True


def m_update(db, pools, rng):
    ts = [t for t in ORDER if db[t]]
    if not ts:
        return False
    t = rng.choice(ts)
    r = rng.choice(db[t])
    cols = [c for c, _ in SCHEMA[t] if c not in KEYCOLS]
    c = rng.choice(cols)
    if rng.random() < 0.5 and len(db[t]) > 1:
        r[c] = rng.choice(db[t])[c]          # tie with another row
    else:
        r[c] = rng.choice(pools[c])
    return True


def m_repoint(db, pools, rng):
    opts = [(t, fc, p) for t in ORDER if db[t] for fc, p in PARENTS[t] if len(db[p]) > 1]
    if not opts:
        return False
    t, fcols, p = rng.choice(opts)
    r = rng.choice(db[t])
    pr = rng.choice(db[p])
    old = dict(r)
    for fc, pc in zip(fcols, PK[p]):
        r[fc] = pr[pc]
    keys = [tuple(x[c] for c in PK[t]) for x in db[t]]
    if len(set(keys)) != len(keys):          # PK contains the FK (partsupp, lineitem's orderkey) -> undo on clash
        r.update(old)
        return False
    # dependants of r (if r's PK changed) must follow
    if tuple(old[c] for c in PK[t]) != tuple(r[c] for c in PK[t]):
        for child, cf in CHILDREN[t]:
            for x in db[child]:
                if tuple(x[c] for c in cf) == tuple(old[c] for c in PK[t]):
                    for c, pc in zip(cf, PK[t]):
                        x[c] = r[pc]
        if not check_constraints(db):
            r.update(old)
            return False
    return True


# ------------------------------------------------------------------------------------------- batch search
def sig(res):
    if isinstance(res, str):
        return "ERR"
    return hashlib.md5(repr(sorted(map(repr, res))).encode()).hexdigest()


class Batch:
    def __init__(self, qid, cands, seed, budget=BUDGET_S, use_sf=True):
        self.t0 = time.perf_counter()
        self.use_sf = use_sf
        self.qid, self.seed, self.budget = qid, seed, budget
        self.rng = random.Random(f"seer-{qid}-{seed}")
        self.orig = QUERIES[str(qid)]
        self.o_sql, o_ord = tiebreak(self.orig)
        self.cands = {}
        for c in cands:
            s_, ordd = tiebreak(c["sql"])
            self.cands[c["rid"]] = {"sql": s_, "raw": c["sql"], "ordered": bool(o_ord) and bool(ordd)}
        self.pools = build_pools([self.orig] + [c["sql"] for c in cands], random.Random(f"pool-{qid}-{seed}"))
        self.con = duckdb.connect(config={"threads": 1})
        self.acon = duckdb.connect(config={"threads": 1})
        self.atoms = Atoms(self.pools, self.acon)
        self.wit = Witness(self.pools, self.atoms, self.rng)
        self.qinfo = {None: QInfo(self.orig)}
        for rid, c in self.cands.items():
            self.qinfo[rid] = QInfo(c["raw"])
        self.live = set(self.cands)
        self.verdict = {}
        self.errs = {rid: 0 for rid in self.cands}
        self.ok_runs = {rid: 0 for rid in self.cands}
        self.n_db = 0
        self.targets = {}      # (qkey, atom_key, pol) -> 'open' | 'covered' | 'infeasible'
        self.stats = {"witness_ok": 0, "witness_unsat": 0, "rounds": 0, "restarts": 0, "sf": 0}

    def elapsed(self):
        return time.perf_counter() - self.t0

    # ---- execution
    def execute(self, db, record=True, timeout=QTIMEOUT, tables=None):
        tabs = tables or to_arrow(db)
        for t, tab in tabs.items():
            self.con.register(t, tab)
        ores, _ = run(self.con, self.o_sql, timeout)
        outs = {None: ores}
        refuted = []
        for rid in sorted(self.live):
            c = self.cands[rid]
            cres, _ = run(self.con, c["sql"], timeout)
            outs[rid] = cres
            if isinstance(cres, str):
                self.errs[rid] += 1
                continue
            self.ok_runs[rid] += 1
            if isinstance(ores, str):
                continue
            if same(ores, cres, c["ordered"]) is False:
                refuted.append(rid)
        if record:
            self.n_db += 1
            for rid in refuted:
                self.refute(rid, db, ores, outs[rid])
        return outs, refuted

    def refute(self, rid, db, ores, cres):
        """Engine-confirmed refutation: constraints asserted, re-executed on a fresh connection."""
        assert db is None or check_constraints(db), "counterexample violates declared constraints"
        if db is not None:
            con = duckdb.connect(config={"threads": 1})
            for t, tab in to_arrow(db).items():
                con.register(t, tab)
            a, _ = run(con, self.o_sql, SF_TIMEOUT); b, _ = run(con, self.cands[rid]["sql"], SF_TIMEOUT)
            con.close()
            assert not isinstance(a, str) and not isinstance(b, str) and same(a, b, self.cands[rid]["ordered"]) is False, \
                f"refutation of Q{self.qid}.{rid} not reproducible"
            size = sum(len(v) for v in db.values())
            cex = {t: [{k: norm(v) for k, v in r.items()} for r in rows] for t, rows in db.items() if rows}
        else:
            size, cex = "sf0.01", None
        self.live.discard(rid)
        self.verdict[rid] = {"verdict": "REFUTED", "t": round(self.elapsed(), 3), "db_index": self.n_db,
                             "cex_rows": size, "cex": cex, "orig_out": str(ores[:3]), "cand_out": str(cres[:3])}

    def score(self, outs):
        live_sigs = [sig(outs[r]) for r in self.live if r in outs]
        o = sig(outs[None])
        return (sum(1 for x in live_sigs if x != o and x != "ERR"), len(set(live_sigs) | {o}),
                0 if isinstance(outs[None], str) or not outs[None] else 1)

    # ---- targets
    def init_targets(self, db):
        for qk in [None] + sorted(self.live):
            r = self.wit.encode(self.qinfo[qk], db, scope_pol={}) if self.qinfo[qk].ok else None
            if r is None:
                continue
            for key in r[1]:
                for pol in (True, False):
                    self.targets.setdefault((qk, key, pol), "open")

    def mark(self, qk, got):
        for key, vals in got.items():
            for v in vals:
                if (qk, key, v) in self.targets:
                    self.targets[(qk, key, v)] = "covered"

    def m_witness(self, db):
        open_t = [k for k, v in self.targets.items() if v == "open" and (k[0] is None or k[0] in self.live)]
        if open_t and self.rng.random() < 0.8:
            qk, key, pol = self.rng.choice(open_t)
            r = None
            for mode in ("mcdc", "loose", "bare"):
                r = self.wit.encode(self.qinfo[qk], db, force=(key, pol), mode=mode)
                if r is not None:
                    break
            if r is None:
                self.targets[(qk, key, pol)] = "infeasible"
                self.stats["witness_unsat"] += 1
                return False
            self.targets[(qk, key, pol)] = "covered"   # only a forced (MC/DC where possible) witness covers
        else:
            qk = self.rng.choice([None] + sorted(self.live))
            if not self.qinfo[qk].ok:
                return False
            nf = len(self.qinfo[qk].formulas)
            pol = {k: (self.rng.random() < 0.8) for k in range(nf)}
            r = self.wit.encode(self.qinfo[qk], db, scope_pol=pol) or self.wit.encode(self.qinfo[qk], db)
            if r is None:
                self.stats["witness_unsat"] += 1
                return False
        rows, got = r
        for t, rs in rows.items():
            db[t].extend(rs)
        assert check_constraints(db), "witness insertion broke constraints"
        self.stats["witness_ok"] += 1
        return True

    def mutate(self, db):
        ops = [(self.m_witness, 0.4), (m_clone, 0.2), (m_update, 0.2), (m_delete, 0.1), (m_repoint, 0.1)]
        new = copy.deepcopy(db)
        for _ in range(self.rng.choice([1, 1, 2, 3])):
            x = self.rng.random()
            for op, w in ops:
                x -= w
                if x <= 0:
                    break
            if op == self.m_witness:
                op(new)
            else:
                op(new, self.pools, self.rng)
        assert check_constraints(new), "mutation broke constraints"
        return new

    def fresh_db(self):
        db = {t: [] for t in ORDER}
        for _ in range(self.rng.randint(1, 3)):
            self.m_witness(db)
        return db

    def run(self):
        # round 0: the dbgen SF0.01 instance (constraint-satisfying by construction)
        if self.use_sf:
            sf = {t: pq.read_table(f"{SF}/{t}.parquet") for t in ORDER}
            outs, ref = self.execute(None, record=False, timeout=SF_TIMEOUT, tables=sf)
            self.n_db += 1
            for rid in ref:
                self.refute(rid, None, outs[None], outs[rid])
            self.stats["sf"] = len(ref)
        empty = {t: [] for t in ORDER}
        self.init_targets(empty)
        db = self.fresh_db()
        outs, _ = self.execute(db)
        cur = self.score(outs)
        last_ref, stagnant, rounds = 0, 0, 0
        seen = set()
        why = "budget"
        while self.live:
            if self.elapsed() > self.budget:
                why = "budget"
                break
            rounds += 1
            best = None
            for _ in range(MUTANTS):
                m = self.mutate(db)
                o, ref = self.execute(m)
                if ref:
                    last_ref = rounds
                if not self.live:
                    break
                sc = self.score(o)
                nov = sum(1 for k, v in o.items() if (k, sig(v)) not in seen)
                for k, v in o.items():
                    seen.add((k, sig(v)))
                key = (sc, nov)
                if best is None or key > best[0]:
                    best = (key, m)
            if not self.live:
                why = "all_refuted"
                break
            if best[0][0] >= cur or best[0][1] > 0 or self.rng.random() < 0.2:
                db, cur = best[1], best[0][0]
                stagnant = 0 if best[0][1] > 0 else stagnant + 1
            else:
                stagnant += 1
            if sum(len(v) for v in db.values()) > MAX_ROWS or stagnant > 25:
                db = self.fresh_db(); stagnant = 0; self.stats["restarts"] += 1
                cur = self.score(self.execute(db)[0])
            open_t = [k for k, v in self.targets.items() if v == "open" and (k[0] is None or k[0] in self.live)]
            if not open_t and rounds >= MIN_ROUNDS and rounds - last_ref >= STALL:
                why = "saturated"
                break
        else:
            why = "all_refuted"
        self.stats["rounds"] = rounds
        end = round(self.elapsed(), 3)
        for rid in self.cands:
            if rid in self.verdict:
                continue
            if self.ok_runs[rid] == 0:
                v = "FAIL"
            else:
                v = "EQ_BOUNDED" if why == "saturated" else "TIMEOUT"
            self.verdict[rid] = {"verdict": v, "t": end, "db_index": self.n_db}
        tg = {}
        for v in self.targets.values():
            tg[v] = tg.get(v, 0) + 1
        return {"qid": self.qid, "seed": self.seed, "n_cands": len(self.cands), "stop": why, "wall": end,
                "n_db": self.n_db, "targets": tg, **self.stats, "verdicts": self.verdict}


def job(args):
    qid, cands, seed, budget, use_sf = args
    try:
        return Batch(qid, cands, seed, budget, use_sf).run()
    except AssertionError:
        raise
    except Exception as e:
        import traceback
        return {"qid": qid, "seed": seed, "crash": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()[-2000:],
                "verdicts": {c["rid"]: {"verdict": "FAIL", "t": None} for c in cands}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rewrites"); ap.add_argument("out")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--budget", type=float, default=BUDGET_S)
    ap.add_argument("--qids", default="")
    ap.add_argument("--inline", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-sf", action="store_true", help="ablation: skip the SF0.01 dbgen instance in round 0")
    a = ap.parse_args()
    rws = [json.loads(l) for l in open(a.rewrites)]
    byq = {}
    for r in rws:
        byq.setdefault(r["qid"], []).append({"rid": r["rid"], "sql": r["sql"]})
    qids = [int(x) for x in a.qids.split(",")] if a.qids else sorted(byq)
    jobs = [(q, byq[q], a.seed, a.budget, not a.no_sf) for q in qids]
    if a.inline:  # child mode: one batch in this process, result as one JSON line on stdout
        for jb in jobs:
            print("RESULT " + json.dumps(job(jb), default=str), flush=True)
        return
    jobs.sort(key=lambda j: -len(QUERIES[str(j[0])]))

    def isolated(jb):
        # one OS process per batch: a native crash (DuckDB/Z3) or hang kills only this batch, never the run
        # (multiprocessing.Pool loses the task and blocks forever when a worker dies -- seen on Q15, 2026-10-18).
        q, cands = jb[0], jb[1]
        cmd = [sys.executable, "-X", "faulthandler", os.path.abspath(__file__), a.rewrites, "-", "--inline",
               "--qids", str(q), "--seed", str(a.seed), "--budget", str(a.budget)] + (["--no-sf"] if a.no_sf else [])
        t = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=a.budget + HARD_EXTRA_S)
            lines = [l[7:] for l in p.stdout.splitlines() if l.startswith("RESULT ")]
            if p.returncode == 0 and lines:
                return json.loads(lines[-1])
            # a soundness assertion in the child must stop the whole run, not be booked as a crash
            assert "AssertionError" not in p.stderr, f"Q{q}: runner assertion failed\n{p.stderr[-3000:]}"
            why = f"exit {p.returncode}: {p.stderr[-1500:]}"
        except subprocess.TimeoutExpired:
            why = f"hard timeout {a.budget + HARD_EXTRA_S}s"
        return {"qid": q, "seed": a.seed, "crash": why, "wall": round(time.time() - t, 3),
                "verdicts": {c["rid"]: {"verdict": "FAIL", "t": None} for c in cands}}

    with ThreadPoolExecutor(a.workers) as ex, open(a.out, "w") as f:
        for res in ex.map(isolated, jobs):
            f.write(json.dumps(res, default=str) + "\n"); f.flush()
            vs = [v["verdict"] for v in res["verdicts"].values()]
            print(res["qid"], res.get("stop", "CRASH " + str(res.get("crash"))[:300]), res.get("wall"),
                  {x: vs.count(x) for x in set(vs)}, flush=True)
    rows = [json.loads(l) for l in open(a.out)]
    assert sorted(r["qid"] for r in rows) == sorted(qids), "missing batches"

if __name__ == "__main__":
    main()
