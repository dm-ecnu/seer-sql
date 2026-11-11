"""SEER: batch equivalence judgement by partition refinement over engine-confirmed distinguishing databases.

State: a partition P of the batch (queries that raise an engine error on a database are not refined there).
  1. fast tier: (a) diff-targeted witness search (seer2.refine.Refiner: predicate/aggregate witnesses over value
     pools, constraint-preserving mutations, per-kind success ranking, greedy selection); each of its instances is
     replayed here through run_db; (b) random Σ-valid databases from the same pools (fast.FastGen). Every database
     is executed on the real backend for all queries of multi-member classes and P is refined (split_class);
  2. symbolic tier for what remains: for k = 1, 2, 4, ... (symbolic.Level), repeatedly ask for a Σ-valid
     database that separates a remaining class (optionally maximize encoded pair gain), execute it, refine P; when the round goal is UNSAT
     the remaining classes are bounded-equivalent at k; then grow k;
  3. stop when every class is a singleton (all_split), when every multi-member class is bounded-equivalent at
     k_max (bounded_equivalent), or on budget (undecided pairs keep their reason).
Every split is decided by the engine (never by the solver) and its database is kept as a certificate; at the end
every certificate is replayed on a fresh backend connection (runner assertion).
"""
from __future__ import annotations

import datetime as dt
import decimal
import itertools
import json
import re
import time

from seer2.backends import EngineRejected, make_backend
from seer2.refine import Out, Prepared, _rkey, approx_same, execute, split_class

import z3

from .encode import Domain, schema_strings
from .fast import FastGen
from seer2.refine import Refiner
from .front import Front
from . import ir
from .ir import Unsupported
from .symbolic import Level

_COUNT_CMP = re.compile(r"COUNT\s*\([^()]*\)\s*(?:>=|<=|<>|!=|>|<|=)\s*(\d+)|(\d+)\s*(?:>=|<=|<>|!=|>|<|=)\s*COUNT\s*\(",
                        re.I)
_LIMIT_N = re.compile(r"\bLIMIT\s+(\d+)(?:\s+OFFSET\s+(\d+))?", re.I)


def nrows(o):
    """Row count of an output when it is fixed although the rows themselves may depend on physical order (a
    nondeterministic output carries it as ("#n", count) when every probed order agreed on it); None otherwise."""
    if o.kind == "ok":
        return len(o.rows) if o.rows is not None else None
    if o.kind == "nondet" and isinstance(o.bag, tuple) and o.bag[:1] == ("#n",):
        return o.bag[1]
    return None


def split_class_n(cls, outs):
    """split_class extended to outputs that depend on physical row order (bare columns, ties at a LIMIT boundary).
    Such a query has several possible results on the instance, an order-independent query exactly one, so the two
    differ there; the split is taken only when the order-dependent query's result in the instance's own load order
    (the one any replay of the certificate observes) differs from the other query's result as well. Two
    order-dependent queries are separated only by an order-invariant row count."""
    if all(outs[q].kind == "ok" for q in cls):
        return split_class(cls, outs)
    if any(outs[q].kind not in ("ok", "nondet") for q in cls):
        return [cls]
    det = [q for q in sorted(cls) if outs[q].kind == "ok"]
    blocks = [list(b) for b in split_class(det, outs)] if det else []
    pool = []
    for q in sorted(cls):
        o = outs[q]
        if o.kind == "ok":
            continue
        home = None
        if o.rows is not None:
            for b in blocks:
                r = outs[b[0]]
                rr = r.rows if not r.ordered else sorted(r.rows, key=_rkey)
                if approx_same(rr, o.rows):
                    home = b
                    break
        else:
            home = blocks[0] if blocks else None      # no load-order result kept: never separated from the rest
        if home is not None:
            home.append(q)
        else:
            pool.append(q)
    if pool:
        ns = {q: nrows(outs[q]) for q in pool}
        if any(v is None for v in ns.values()):
            blocks.append(pool)
        else:
            g = {}
            for q in pool:
                g.setdefault(ns[q], []).append(q)
            blocks += list(g.values())
    if len(blocks) == 1:
        return [cls]
    return [frozenset(b) for b in blocks]


_ORDER_SENS = re.compile(r"\b(ROW_NUMBER|FIRST_VALUE|LAST_VALUE|NTH_VALUE|LAG|LEAD|ANY_VALUE|ARBITRARY|ROWS\s+BETWEEN|ROWS\s+UNBOUNDED)\b")


def loadable_order(S, db, report=False):
    """Rows of a self-referencing table, parents first: the instance is a set of rows, but a loader that inserts row
    by row with immediate FK checks (the validator, any engine without deferred constraints) needs a parent to
    precede the rows referencing it. Rows on a reference cycle keep their order (no row-by-row load exists);
    with report=True the result is (db, acyclic)."""
    out, acyclic = None, True
    for t, rows in db.items():
        tb = S.tables.get(t)
        fks = [fk for fk in (tb.fks if tb else []) if fk.ref == t]
        if not fks or len(rows) < 2:
            continue
        keys = [{tuple(r.get(c) for c in fk.ref_cols) for r in rows} for fk in fks]
        done = [set() for _ in fks]
        rest, order = list(rows), []
        while rest:
            nxt = []
            for r in rest:
                ok = True
                for j, fk in enumerate(fks):
                    v = tuple(r.get(c) for c in fk.cols)
                    if any(x is None for x in v) or v not in keys[j] or v in done[j] \
                            or v == tuple(r.get(c) for c in fk.ref_cols):
                        continue
                    ok = False
                    break
                if ok:
                    order.append(r)
                    for j, fk in enumerate(fks):
                        done[j].add(tuple(r.get(c) for c in fk.ref_cols))
                else:
                    nxt.append(r)
            if len(nxt) == len(rest):
                order += nxt
                acyclic = False
                break
            rest = nxt
        if order != list(rows):
            out = out or dict(db)
            out[t] = order
    return (out or db, acyclic) if report else (out or db)


def _jsonable(db):
    def enc(v):
        if isinstance(v, decimal.Decimal):
            return {"$dec": str(v)}
        if isinstance(v, dt.datetime):
            return {"$ts": v.isoformat()}
        if isinstance(v, dt.date):
            return {"$date": v.isoformat()}
        return v
    return {t: [{c: enc(v) for c, v in r.items()} for r in rows] for t, rows in db.items()}


def from_jsonable(db):
    def dec(v):
        if isinstance(v, dict):
            if "$dec" in v:
                return decimal.Decimal(v["$dec"])
            if "$ts" in v:
                return dt.datetime.fromisoformat(v["$ts"])
            if "$date" in v:
                return dt.date.fromisoformat(v["$date"])
        return v
    return {t: [{c: dec(v) for c, v in r.items()} for r in rows] for t, rows in db.items()}


class Stats(dict):
    def add(self, k, v=1):
        self[k] = self.get(k, 0) + v


class _AllSplit(Exception):
    """Raised from the refiner callback once every class is a singleton: nothing is left to refine."""


class Seer:
    def __init__(self, schema, queries, dialect="duckdb", backend="duckdb", budget=120.0, seed=0,
                 fast_dbs=200, fast_stagnation=60, levels=(1, 2, 4), solver_timeout_ms=20000, max_cost=400000,
                 qtimeout=10.0, symbolic=True, fast=True, trace=None, prover=None, refine_frac=0.5, refine_rounds=3,
                 reference=None, max_k=16, deepen_frac=0.7, close_idle=3, symbolic_selection="representatives"):
        if symbolic_selection not in ("representatives", "max_pairs"):
            raise ValueError("unknown symbolic selection: " + symbolic_selection)
        self.symbolic_selection = symbolic_selection
        self.symbolic_selection_trace = []
        self.refine_frac, self.refine_rounds = refine_frac, refine_rounds
        self.max_k, self.deepen_frac, self.close_idle = max_k, deepen_frac, close_idle
        # the reference query (default: the first, the convention of result()); the refine tier aims its witness
        # search at the reference's star, as the v2 prototype does when it is given the gold query
        self.reference = reference if reference is not None else (queries[0][0] if queries else None)
        self.S, self.dialect, self.backend_name = schema, dialect, backend
        self.budget, self.seed = budget, seed
        self.fast_dbs, self.fast_stagnation = fast_dbs, fast_stagnation
        self.levels, self.solver_timeout_ms, self.max_cost = tuple(levels), solver_timeout_ms, max_cost
        self.use_symbolic, self.use_fast = symbolic, fast
        self.trace = trace
        # prover hook (stub): prover(schema, [sql, ...], dialect) -> True when it proves the class equivalent for
        # all databases, None otherwise; called on classes that survive both tiers.
        self.prover = prover
        self.full = set()            # frozenset(class) proved equivalent by the prover
        self.t0 = time.perf_counter()
        self.stats = Stats()
        self.timing = Stats()
        self.backend = make_backend(backend, schema, qtimeout)
        self.front = Front(schema, dialect)
        self.queries = {}            # id -> sql
        self.prep = {}
        self.ir = {}
        self._osens = {}
        self.unsupported = {}        # id -> (code, detail)
        self.partition = []          # list of lists
        self.dbs = []                # certificate store: databases that refined P (json-able)
        self.events = []             # (db index, class, blocks)
        self.dropped = set()         # positions in events whose replay never reproduced (withdrawn)
        self.recheck_tries = 3
        self.last_split_s = 0.0      # time of the last refinement: when the final partition was reached
        self.outs = {}               # (db index, qid) -> Out
        self.proved = {}             # frozenset(class) -> k (bounded-equivalent at k)
        self.reasons = {}            # frozenset(class) -> reason it is undecided
        self.stop = None
        self.invalid = {}            # id -> engine message: the engine rejects the query on the empty database
        for qid, sql in queries:
            self._register(qid, sql)
        self._screen(list(self.queries))
        ok = [q for q in self.queries if q not in self.invalid]
        self.partition = ([ok] if ok else []) + [[q] for q in self.invalid]

    # .................................................................. bookkeeping
    def elapsed(self):
        return time.perf_counter() - self.t0

    def log(self, *a):
        if self.trace:
            self.trace(f"[{self.elapsed():7.2f}s]", *a)

    def _register(self, qid, sql):
        assert qid not in self.queries, f"duplicate query id {qid}"
        self.queries[qid] = sql
        self.prep[qid] = Prepared(sql, self.dialect, self.backend.dialect)
        t0 = time.perf_counter()
        try:
            self.ir[qid] = self.front.translate(sql)
        except Unsupported as u:
            self.unsupported[qid] = (u.code, u.detail[:200])
        except RecursionError:
            self.unsupported[qid] = ("too_deep", "query nesting exceeds the translator's recursion limit")
        self.timing.add("front_s", time.perf_counter() - t0)

    def _screen(self, qs):
        """Queries the engine rejects on the empty database (parse/bind/type errors) can never be compared; they are
        set apart as singletons with verdict query_error so that they do not block refinement of their class."""
        empty = {t: [] for t in self.S.order}
        inst = self.backend.open(empty)
        try:
            for q in qs:
                o = execute(inst, self.prep[q])
                self.stats.add("executions")
                if o.kind == "err":
                    self.invalid[q] = (o.msg or "")[:200]
        finally:
            inst.close()

    def multi(self):
        return [c for c in self.partition if len(c) > 1]

    def unsplit_pairs(self):
        return sum(len(c) * (len(c) - 1) // 2 for c in self.partition)

    # .................................................................. engine
    def run_db(self, db, origin):
        """Execute db for all queries of multi-member classes, refine P. Returns number of newly split pairs."""
        live = [q for c in self.multi() for q in c]
        if not live:
            return 0
        db, acyclic = loadable_order(self.S, db, report=True)
        if not acyclic and not getattr(self.backend, "deferred_fks", True):
            # a self-reference cycle is Σ-valid as a set of rows, but an engine without deferred FK checks (MySQL)
            # cannot hold it as a loaded state: never a certificate there
            self.stats.add("cyclic_reference_skipped")
            return 0
        t0 = time.perf_counter()
        try:
            inst = self.backend.open(db)
        except EngineRejected as e:
            # a Σ-valid instance the engine refuses (a type bound the model does not express): never a certificate;
            # counted so that a non-zero count is visible in every result record
            self.stats.add(f"engine_rejected_{origin}")
            self.log(f"{origin}: engine rejected a Σ-valid instance: {str(e)[:200]}")
            return 0
        try:
            outs = self._exec_all(inst, db, live)
        finally:
            inst.close()
        self.timing.add("exec_s", time.perf_counter() - t0)
        before = self.unsplit_pairs()
        newp, idx = [], None
        for c in self.partition:
            if len(c) < 2:
                newp.append(c)
                continue
            blocks = split_class_n(c, outs)
            if len(blocks) > 1:
                if idx is None:
                    idx = len(self.dbs)
                    self.dbs.append(_jsonable(db))
                    self.stats.add(f"cert_dbs_{origin}")
                self.events.append((idx, sorted(c), [sorted(b) for b in blocks]))
                self.last_split_s = self.elapsed()
                for q in c:
                    self.outs[idx, q] = outs[q]
                newp += [sorted(b) for b in blocks]
            else:
                newp.append(c)
        self.partition = newp
        gained = before - self.unsplit_pairs()
        if gained:
            self.log(f"{origin}: split {gained} pairs, classes={len(self.partition)}")
        return gained

    # .................................................................. tiers
    def fast_tier(self):
        if not self.use_fast or not self.multi():
            return
        t0 = time.perf_counter()
        self.refine_tier()
        self.timing.add("refine_s", time.perf_counter() - t0)
        gen = FastGen(self.S, list(self.queries.values()), self.dialect, self.seed)
        stagn = 0
        for i in range(self.fast_dbs):
            if not self.multi() or self.elapsed() > self.budget * 0.4 or stagn >= self.fast_stagnation:
                break
            db = gen.next()
            self.stats.add("fast_dbs")
            g = self.run_db(db, "fast")
            stagn = 0 if g else stagn + 1
        self.timing.add("fast_s", time.perf_counter() - t0)

    def refine_tier(self):
        """Diff-targeted witness search (the seer2 refiner): targets are the syntactic differences inside each class,
        realised by predicate witnesses over the value pools, aggregate witnesses (group/HAVING/DISTINCT/NULL
        sensitivity) and constraint-preserving mutations, ranked by a per-kind success estimate and selected greedily
        by pairs split. Its instances are not trusted: each one that refined its own partition is replayed here
        through run_db, i.e. the same engine execution, nondeterminism probe and certificate store as every other
        tier, so a split is accepted only if it is reproduced on the engine under Seer's rules."""
        # The refiner's search is randomised and ends when its targets saturate; a saturated run that leaves classes
        # unsplit is restarted with a fresh seed on the remaining classes while the tier's budget lasts (at most
        # `refine_rounds` runs), so a split one random run happens to miss is not lost to sampling.
        # the budget is split by what each tier can act on: the symbolic tier only splits queries inside the
        # encoding, so the refine tier's share grows from refine_frac (every live query encodable) to the whole
        # budget (none encodable) in one continuous run; a restarted refiner would lose its search state
        live = [q for c in self.multi() for q in c]
        enc = sum(1 for q in live if q not in self.unsupported) / max(1, len(live))
        frac = 1.0 - enc * (1.0 - self.refine_frac)
        self.stats.add("refine_share_pct", round(100 * frac))
        self._refine_rounds(time.perf_counter() + self.budget * frac, 0)

    def _refine_rounds(self, t_end, first, open_only=False):
        for rnd in range(first, first + self.refine_rounds):
            live = [q for c in self.multi() if not (open_only and self._settled(c)) for q in c]
            left = t_end - time.perf_counter()
            if len(live) < 2 or left < 1.0:
                return
            stop = self._refine_round(live, left, self.seed + 7919 * rnd)
            self.stats.add("refine_rounds")
            if stop != "saturated":
                return

    def _open(self):
        return [q for c in self.multi() if not self._settled(c) for q in c]

    def _close(self, t_end):
        """Budget the symbolic tier left goes to the classes it neither split nor bounded: refine rounds with fresh
        seeds alternate with bursts of random constraint-valid databases until the budget ends or `close_idle`
        consecutive cycles split nothing (the search has saturated on these classes)."""
        rnd, idle = self.refine_rounds, 0
        while len(self._open()) >= 2 and t_end - time.perf_counter() > 1.0 and idle < self.close_idle:
            before = self.unsplit_pairs()
            self._refine_rounds(t_end, rnd, open_only=True)
            rnd += self.refine_rounds
            live = self._open()
            if len(live) >= 2 and t_end - time.perf_counter() > 1.0:
                gen = FastGen(self.S, [self.queries[q] for q in live], self.dialect, self.seed + 104729 * rnd)
                for _ in range(self.fast_dbs):
                    if len(self._open()) < 2 or time.perf_counter() > t_end:
                        break
                    self.stats.add("close_fast_dbs")
                    self.run_db(gen.next(), "close")
            self.stats.add("close_cycles")
            idle = idle + 1 if self.unsplit_pairs() == before else 0

    def _settled(self, c):
        return frozenset(c) in self.proved or frozenset(c) in self.full

    def _refine_round(self, live, budget, seed):
        done = set()

        def confirm(i, db):
            # each instance is confirmed on the engine the moment the refiner adopts it, so a split is recorded
            # (and its time-to-split measured) when found, not after the refiner's run ends
            if i in done or not self.multi():
                return
            done.add(i)
            viol = self.S.violations(db)
            assert not viol, f"refiner instance violates Σ: {viol[:3]}"
            self.stats.add("refine_dbs")
            self.run_db(db, "refine")
            if not self.multi():
                raise _AllSplit()

        try:
            rf = Refiner(self.S, [(q, self.queries[q]) for q in live], backend=self.backend_name,
                         dialect=self.dialect, budget=budget, seed=seed, M=4, delta=0.05, on_db=confirm,
                          reference=self.reference if self.reference in live else None)
        except Exception as ex:          # a heuristic tier: its failure costs recall only, never soundness
            self.stats.add(f"refine_crash:{type(ex).__name__}")
            return "crash"
        stop = "crash"
        try:
            res = rf.run()
            stop = res["stop"]
            self.stats.add("refine_exec", res.get("n_executed", 0))
            self.log(f"refine: stop={res['stop']} dbs={len(rf.dbs)}")
        except AssertionError as ex:
            # the refiner's own end-of-run replay (seer2 verify) failing is a refiner failure, not a soundness
            # failure of SEER: every instance it adopted was confirmed by run_db and is replayed again by recheck
            if not str(ex).startswith("split on DB#"):
                raise
            self.stats.add("refine_crash:verify")
            self.stats.add("refine_exec", rf.n_exec)
        except _AllSplit:
            stop = "all-split"
            self.stats.add("refine_exec", rf.n_exec)
        except Exception as ex:
            # instances found before the failure are still replayed (and checked) below
            self.stats.add(f"refine_crash:{type(ex).__name__}")
            self.stats.add("refine_exec", rf.n_exec)
        for ev in rf.events:             # adopted before a crash, or skipped by the callback: confirm now
            confirm(ev["db"], rf.dbs[ev["db"]])
        return stop

    def level_schedule(self):
        """Witness sizes k for the symbolic tier: the configured levels, then sizes the queries' own constants call
        for (COUNT(..) compared with c needs c or c+1 rows in a group; LIMIT n [OFFSET m] needs n+m+1 rows), then
        doubling (8, 16, ...) up to max_k: a class that is bounded-equivalent at a small k keeps being searched at
        larger k while the budget lasts instead of being reported early."""
        ks = set(self.levels)
        sql = " ".join(self.queries[q] for c in self.multi() for q in c)
        for m in _COUNT_CMP.finditer(sql):
            c = int(m.group(1) or m.group(2))
            ks |= {c, c + 1}
        for m in _LIMIT_N.finditer(sql):
            ks.add(int(m.group(1)) + int(m.group(2) or 0) + 1)
        k = max(self.levels) * 2
        while k <= self.max_k:
            ks.add(k)
            k *= 2
        return sorted(x for x in ks if 1 <= x <= self.max_k)

    def symbolic_tier(self):
        if not self.use_symbolic:
            return
        base = max(self.levels)
        for k in self.level_schedule():
            if not self.multi() or self.elapsed() > self.budget:
                break
            # beyond the configured levels the tier deepens only while the refine tier keeps a share of the budget
            # for classes outside the encoding
            if k > base and self.elapsed() > self.budget * self.deepen_frac:
                break
            irs = {q: self.ir[q] for c in self.multi() for q in c if q in self.ir}
            if not irs:
                break
            t0 = time.perf_counter()
            dom = Domain.build(list(irs.values()), checks_strings=schema_strings(self.S))
            try:
                lv = Level(self.S, self.front, dom, irs, k, self.dialect, self.solver_timeout_ms, self.max_cost,
                           self.seed, deadline=self.t0 + self.budget,
                           per_query_s=max(2.0, self.budget / 12))
            except MemoryError:
                self.stats.add("level_oom")
                break
            except (Unsupported, z3.Z3Exception, RecursionError) as ex:
                # Σ itself is outside the fragment: no level can be built; every open class stays undecided
                why = f"{ex.code}: {ex}" if isinstance(ex, Unsupported) else type(ex).__name__
                for c in self.multi():
                    self.reasons[frozenset(c)] = f"k={k}: schema constraints not encodable ({why})"
                self.stats.add("sigma_unsupported")
                break
            self.timing.add("encode_s", lv.encode_s)
            self.stats.add("levels")
            mismatch = 0
            while self.multi() and self.elapsed() < self.budget:
                classes = []
                for c in self.multi():
                    enc = [q for q in c if lv.ok(q)]
                    if len(enc) > 1:
                        classes.append(enc)
                if not classes:
                    break
                try:
                    status, m, pred = lv.find(classes, budget_s=max(1.0, (self.budget - self.elapsed()) / 4),
                                              selection=self.symbolic_selection)
                    if self.symbolic_selection == "max_pairs":
                        self.symbolic_selection_trace.append({"k": k, **lv.selection_info})
                except (TimeoutError, MemoryError):
                    status, m, pred = "unknown", None, None
                self.stats.add("solver_calls", lv.checks)
                lv.checks = 0
                if status == "unsat":
                    if mismatch:
                        # Database blockers remove legal inputs. UNSAT of that
                        # restricted search cannot prove the original domain.
                        for c in self.multi():
                            self.reasons[frozenset(c)] = f"k={k}: excluded_unconfirmed_models"
                        self.stats.add("bounded_proof_withheld")
                        self.log(f"k={k}: restricted UNSAT after model mismatch; proof withheld")
                        break
                    for c in self.multi():
                        enc = [q for q in c if lv.ok(q)]
                        if len(enc) == len(c):
                            self.proved[frozenset(c)] = k
                        else:
                            bad = {q: (lv.unsup.get(q) or "unsupported") for q in c if not lv.ok(q)}
                            self.reasons[frozenset(c)] = f"k={k}: not encodable: {bad}"
                    self.log(f"k={k}: UNSAT -> remaining classes bounded-equivalent at k")
                    break
                if status == "unknown":
                    for c in self.multi():
                        self.reasons[frozenset(c)] = f"k={k}: solver unknown/timeout"
                    self.stats.add("solver_unknown")
                    break
                db = lv.decode(m, self.S)
                viol = self.S.violations(db)
                assert not viol, f"symbolic model violates Σ: {viol[:3]}"
                self.stats.add("symbolic_dbs")
                g = self.run_db(db, f"sym_k{k}")
                predicted = sum(len(b) for bl in pred for b in bl) - sum(1 for bl in pred for _ in bl)
                if g == 0:
                    mismatch += 1
                    self.stats.add("model_mismatch")
                    self.log(f"k={k}: model predicted a split the engine did not confirm ({mismatch})")
                    if mismatch >= 3:
                        for c in self.multi():
                            self.reasons[frozenset(c)] = f"k={k}: encoding/engine mismatch"
                        break
                    # exclude this exact database and retry
                    lv.solver.add(_block(lv, m))
            self.timing.add("solve_s", lv.solve_s)
            self.timing.add("level_s", time.perf_counter() - t0)

    # .................................................................. API
    def run(self):
        if len(self.queries) < 2:
            self.stop = "trivial"
            return self.result()
        self.fast_tier()
        resource_stop = False
        try:
            self.symbolic_tier()
        except z3.Z3Exception as exc:
            if "out of memory" not in str(exc).casefold():
                raise
            # The accepted database store survives a failed symbolic proposal.
            # Stop using the exhausted solver; fresh-engine recheck still decides
            # which earlier splits can be reported.
            resource_stop = True
            self.stats.add("solver_out_of_memory")
            for c in self.multi():
                if not self._settled(c):
                    self.reasons[frozenset(c)] = "solver_out_of_memory"
        # budget the symbolic tier did not use goes back to the refine tier, on the classes it neither split nor
        # proved bounded-equivalent (typically classes with constructs outside the encoding: the refine tier is then
        # the only tier that can split them, and it gets the whole budget, as the v2 prototype does)
        if not resource_stop and self.use_fast and any(not self._settled(c) for c in self.multi()):
            t0 = time.perf_counter()
            self._close(self.t0 + self.budget)
            self.timing.add("refine_close_s", time.perf_counter() - t0)
        if not resource_stop and self.prover is not None:
            for c in self.multi():
                t0 = time.perf_counter()
                if self.prover(self.S, [self.queries[q] for q in c], self.dialect) is True:
                    self.full.add(frozenset(c))
                self.timing.add("prover_s", time.perf_counter() - t0)
        if not self.multi():
            self.stop = "all_split"
        elif resource_stop:
            self.stop = "resource_limit"
        elif all(frozenset(c) in self.proved or frozenset(c) in self.full for c in self.multi()):
            self.stop = "bounded_equivalent"
        elif self.elapsed() >= self.budget:
            self.stop = "budget"
        else:
            self.stop = "undecided"
        self.recheck()
        return self.result()

    def _order_sensitive(self, q):
        """Queries whose result may depend on physical row order (row_number / ROWS frames / value windows / bare
        columns resolved as any_value): they are also run on a row-reversed copy of the instance."""
        if q not in self._osens:
            hit = bool(_ORDER_SENS.search(self.queries[q].upper()))
            r = self.ir.get(q)
            if r is not None and not hit:
                hit = any(getattr(a, "func", None) == "any_value"
                          for x in ir.walk_rel(r) if x.op == "agg" for a in x.args[2])
            self._osens[q] = hit
        return self._osens[q]

    def _exec_all(self, inst, db, qs):
        outs = {}
        for q in qs:
            outs[q] = execute(inst, self.prep[q])
            self.stats.add("executions")
        probe = [q for q in qs if outs[q].kind == "ok" and self._order_sensitive(q)]
        if probe:
            rev = self.backend.open({t: list(reversed(rows)) for t, rows in db.items()})
            try:
                for q in probe:
                    o2 = execute(rev, self.prep[q])
                    self.stats.add("executions_order_probe")
                    if o2.kind != "ok" or len(split_class(["x", "y"], {"x": outs[q], "y": o2})) > 1:
                        o1 = outs[q]
                        same_n = o2.kind == "ok" and len(o2.rows) == len(o1.rows)
                        rows1 = o1.rows if not o1.ordered else sorted(o1.rows, key=_rkey)
                        outs[q] = Out("nondet", bag=("#n", len(o2.rows)) if same_n else None, lst=o1.bag,
                                      rows=rows1, msg="result depends on physical row order")
                        self.stats.add("nondet_order")
            finally:
                rev.close()
        return outs

    def add_query(self, qid, sql):
        """Incremental: place a new query by replaying the certificate store, then refine its class."""
        self._register(qid, sql)
        self._screen([qid])
        if qid in self.invalid:
            self.partition.append([qid])
            return self.result()
        placed = None
        sigs = {}
        for i, db in enumerate(self.dbs):
            d = from_jsonable(db)
            inst = self.backend.open(d)
            try:
                sigs[i] = self._exec_all(inst, d, [qid])[qid]
            finally:
                inst.close()
        for c in self.partition:
            rep = c[0]
            if all(self._same(i, rep, sigs[i]) for i in range(len(self.dbs))):
                placed = c
                break
        if placed is None:
            self.partition.append([qid])
        else:
            placed.append(qid)
            self.proved.pop(frozenset(placed[:-1]), None)
        self.stop = None
        return self.run()

    def _same(self, i, rep, out):
        o = self.outs.get((i, rep))
        if o is None:
            return True
        return split_class_n([rep, "_new"], {rep: o, "_new": out}).__len__() == 1

    def recheck(self):
        """Replay every certificate on a fresh connection; the refinement it caused must be reproduced."""
        t0 = time.perf_counter()
        for pos, (idx, cls, blocks) in enumerate(self.events):
            db = from_jsonable(self.dbs[idx])
            for attempt in range(self.recheck_tries):
                inst = self.backend.open(db)
                try:
                    outs = self._exec_all(inst, db, cls)
                finally:
                    inst.close()
                got = sorted(sorted(b) for b in split_class_n(cls, outs))
                if got == sorted(blocks):
                    self.stats.add("recheck_ok")
                    break
                # a replay that errs (a statement timeout on a loaded machine) or differs is retried; one that never
                # reproduces is withdrawn: the pairs it alone separated are reported undecided, never split
                self.stats.add("recheck_err" if any(o.kind != "ok" for o in outs.values()) else "recheck_differs")
            else:
                self.dropped.add(pos)
                self.stats.add("recheck_dropped")
                self.log(f"certificate {idx} not reproduced in {self.recheck_tries} replays: withdrawn")
        self.timing.add("recheck_s", time.perf_counter() - t0)

    def pair_certificate(self, a, b):
        for pos, (idx, cls, blocks) in enumerate(self.events):
            if pos in self.dropped:
                continue
            if a in cls and b in cls:
                ba = next(i for i, bl in enumerate(blocks) if a in bl)
                bb = next(i for i, bl in enumerate(blocks) if b in bl)
                if ba != bb:
                    return idx
        return None

    def verdict(self, a, b):
        for q in (a, b):
            if q in self.invalid:
                return {"verdict": "undecided", "reason": "query_error"}
        ca = next(c for c in self.partition if a in c)
        if b not in ca:
            cert = self.pair_certificate(a, b)
            if cert is None:
                return {"verdict": "undecided", "reason": "certificate_not_reproduced"}
            return {"verdict": "split", "cert": cert}
        key = frozenset(ca)
        if key in self.full:
            return {"verdict": "proved_equivalent"}
        if key in self.proved:
            return {"verdict": "bounded_equivalent", "k": self.proved[key]}
        for q in (a, b):
            if q in self.unsupported:
                return {"verdict": "undecided", "reason": f"unsupported:{self.unsupported[q][0]}"}
        return {"verdict": "undecided", "reason": self.reasons.get(key, self.stop or "budget")}

    def result(self):
        ids = list(self.queries)
        verdicts = {}
        # all pairs for ordinary batches; beyond 60 queries (quadratic output) only the pairs with the first query,
        # the reference by convention; any other pair is available through verdict(a, b)
        prs = itertools.combinations(ids, 2) if len(ids) <= 60 else [(ids[0], b) for b in ids[1:]]
        for a, b in prs:
            verdicts[f"{a}|{b}"] = self.verdict(a, b)
        return {
            "stop": self.stop,
            "symbolic_selection": self.symbolic_selection,
            "symbolic_selection_trace": self.symbolic_selection_trace,
            "queries": dict(self.queries),
            "wall": round(self.elapsed(), 3),
            "partition_s": round(self.last_split_s, 3),
            "classes": self.partition,
            "verdicts": verdicts,
            "proved": [{"class": sorted(c), "k": k} for c, k in self.proved.items()],
            "unsupported": {q: list(v) for q, v in self.unsupported.items() if q not in self.invalid},
            "query_error": dict(self.invalid),
            "certificates": {"dbs": self.dbs,
                             "events": [e for pos, e in enumerate(self.events) if pos not in self.dropped]},
            "stats": dict(self.stats),
            "timing": {k: round(v, 3) for k, v in self.timing.items()},
        }


def _block(lv, m):
    """Clause excluding the base-table assignment of model m (existence bits and cells)."""
    import z3
    from .encode import _or
    lits = []
    for t, rows in lv.enc.vars.items():
        for ex, cells in rows:
            lits.append(ex != m.eval(ex, model_completion=True))
            for var, nul in cells:
                lits.append(var != m.eval(var, model_completion=True))
                if not z3.is_false(nul):
                    lits.append(nul != m.eval(nul, model_completion=True))
    return _or(lits)


def replay(schema, res, dialect="duckdb", backend="duckdb", qtimeout=10.0):
    """Re-execute every certificate event of a stored result on a fresh backend; returns the number reproduced."""
    if not res.get("certificates"):
        return 0
    be = make_backend(backend, schema, qtimeout)
    prep = {q: Prepared(sql, dialect, be.dialect) for q, sql in res["queries"].items()}
    n = 0
    for idx, cls, blocks in res["certificates"]["events"]:
        inst = be.open(from_jsonable(res["certificates"]["dbs"][idx]))
        try:
            outs = {q: execute(inst, prep[q]) for q in cls}
        finally:
            inst.close()
        got = sorted(sorted(b) for b in split_class_n(cls, outs))
        assert got == sorted(sorted(b) for b in blocks), f"batch {res.get('batch')}: certificate {idx} not reproduced"
        n += 1
    return n


def judge_batch(schema, queries, **kw):
    return Seer(schema, queries, **kw).run()


def judge_pair(schema, q1, q2, **kw):
    r = Seer(schema, [("q1", q1), ("q2", q2)], **kw).run()
    r["pair"] = r["verdicts"]["q1|q2"]
    return r
