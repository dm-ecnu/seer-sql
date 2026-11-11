"""Streaming partition over engine-replayed witnesses and scoped proof edges.

The partition index contains observations, including pending queries.  Only an
admitted evidence path gives a member a proved/bounded relation to its current
representative. No separation is inferred from an equivalence path.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
import copy
import datetime as dt
import decimal
import math
import os
import json
import pickle
import tempfile
from pathlib import Path
import random
import resource
import time

import sqlglot
from sqlglot import exp
from seer2.backends import QueryError, make_backend
from seer2.refine import Prepared
from .representative_search import RepresentativeSearch
from . import ir

CONTRACT = 'canonical-exact-decimal-float3dp-list-bag-errors-v3'


def value_key(v):
    """Typed, hashable total key; preserve whitespace and duplicate rows."""
    if v is None:
        return ('null', '')
    if isinstance(v, bool):
        return ('number', decimal.Decimal(int(v)))
    if isinstance(v, int):
        return ('number', decimal.Decimal(v))
    if isinstance(v, decimal.Decimal):
        if not v.is_finite():
            return ('nonfinite', str(v).lower())
        # Decimal equality/hash already ignore trailing zeros. normalize()
        # rounds under the ambient decimal context for >28 significant digits.
        return ('number', v if v else decimal.Decimal(0))
    if isinstance(v, float):
        if not math.isfinite(v):
            return ('nonfinite', str(v).lower())
        # Official SQLWitness compares FLOAT/DOUBLE after rounding to 3 places.
        # DECIMAL/NUMERIC and INTEGER retain exact values above.
        rounded = decimal.Decimal(str(round(v, 3)))
        return ('number', rounded if rounded else decimal.Decimal(0))
    if isinstance(v, dt.timedelta):
        return value_key(v.total_seconds())
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return ('temporal', v.isoformat())
    if isinstance(v, str):
        return ('text', v)
    if isinstance(v, (list, tuple)):
        return ('array', tuple(value_key(x) for x in v))
    if isinstance(v, dict):
        return ('object', tuple(sorted((value_key(k), value_key(x)) for k, x in v.items())))
    if isinstance(v, (bytes, bytearray, memoryview)):
        return ('bytes', bytes(v))
    raise TypeError('unsupported engine value: ' + type(v).__name__)


def result_key(rows, ncols, ordered):
    mode = 'list' if ordered else 'bag'
    if isinstance(rows, QueryError):
        return (mode, 'error', rows.msg)
    values = tuple(tuple(value_key(v) for v in row) for row in rows)
    return (mode, 'rows', ncols, values if ordered else tuple(sorted(values)))


@dataclass
class Observation:
    key: tuple
    stable: bool = True
    reason: str = ''
    cardinality: tuple | None = None


def cardinality_key(key):
    return (key[0], 'cardinality', key[2], len(key[3])) if key[1] == 'rows' else key


def observe(inst, prepared, count):
    def run(sql):
        count()
        return inst.run(sql)
    rows, ncols = run(prepared.sql)
    key = result_key(rows, ncols, prepared.ordered)
    if isinstance(rows, QueryError):
        # Resource failures do not define a database-dependent SQL value.
        text = rows.msg.lower()
        stable = not any(s in text for s in (
            'timeout', 'timed out', 'interrupted', 'canceling statement',
            'lost connection', 'server has gone away', 'connection refused',
            'connection reset', 'broken pipe', 'too many connections'))
        return Observation(key, stable, '' if stable else 'execution_resource_limit', key if stable else None)
    if prepared.ordered and prepared.tree is not None:
        asc, desc = [run(sql) for sql in prepared.variants(ncols)]
        if any(isinstance(x[0], QueryError) for x in (asc, desc)):
            return Observation(key, False, 'tie_probe_failed')
        # A list requires a determinate output order. LIMIT boundary ties are
        # kept in the symbolic search; a physical arbitrary tie is no certificate.
        if result_key(*asc, True) != result_key(*desc, True):
            return Observation(key, False, 'order_or_limit_tie',
                cardinality_key(key) if cardinality_key(result_key(*asc, True)) == cardinality_key(result_key(*desc, True)) == cardinality_key(key) else None)
    return Observation(key, cardinality=cardinality_key(key))


@dataclass
class Witness:
    database: dict
    domain: str
    k: int
    observations: dict = field(default_factory=dict)
    active: bool = True
    reason: str = ''
    mode: str = 'full'


@dataclass
class Edge:
    a: str
    b: str
    status: str
    receipt: dict
    k: int | None = None
    domain: str | None = None


class StreamPartition:
    def __init__(self, schema, dialect='sqlite', backend='sqlite', budget_s=600,
                 seed=0, qtimeout=10., bounds=None, prover=None,
                 prover_admit=None, random_dbs=2, search=None, per_call_s=60,
                 quick_witness=None, start_time=None, random_representative=False,
                 quick_failure_cap=None, remainder_quick_comparisons=False,
                 no_certificate_transfer=False, single_bucket_target=False,
                 no_fingerprint_grouping=False, no_pool_reuse=False):
        if not math.isfinite(budget_s) or budget_s < 0:
            raise ValueError('finite nonnegative batch budget required')
        self.schema, self.dialect = schema, dialect
        self.backend = make_backend(backend, schema, qtimeout) if isinstance(backend, str) else backend
        self.search = search or RepresentativeSearch(schema, dialect, seed=seed, persistent_worker=True)
        self.bounds = bounds
        if not math.isfinite(per_call_s) or per_call_s <= 0:
            raise ValueError('positive finite per-call budget required')
        self.per_call_s = per_call_s
        self.adjacency = {}
        self._numeric_preflight = {}
        self.prover = prover
        self.prover_admit = prover_admit or getattr(prover, 'admit', None)
        self.quick_witness = quick_witness
        if quick_failure_cap is not None and quick_failure_cap < 1:
            raise ValueError('positive quick-witness failure cap required')
        self.quick_failure_cap = quick_failure_cap
        self.quick_failure_streak = {}
        self.started = time.perf_counter() if start_time is None else start_time
        self.deadline = self.started + budget_s
        # The final invariant replay and result preparation are charged to B.
        self.finalization_reserve_s = min(20.0, max(1.0, budget_s * 0.035))
        self.cpu_started = self._cpu()
        self.queries, self.prepared, self.complexity = {}, {}, {}
        self.arrival, self.witnesses, self.edges, self.events = [], [], [], []
        self.index, self.representatives, self.fingerprints = {}, {}, {}
        self.pending_reason = {}
        self._witness_origin = {}
        self._last_origin = None
        self._quick_admitted = {}
        self._quick_admission_columns = {}
        self._quick_ran = set()
        self._quick_query_pairs = set()
        self._compared = set()
        # Supervision correction (10-29): the default arrival dispatch compares
        # a new query only once against each existing class representative.
        # Pair-by-pair comparisons of the remaining in-class members at the end
        # of the arrival stage exist solely behind this ablation switch.
        self.remainder_quick_comparisons = remainder_quick_comparisons
        # Experiment-card ablations (10-29): disable certificate transfer
        # (followers re-executed instead of inheriting an edge) and disable the
        # multi-bucket distinguishing target (one bucket per search).
        self.no_certificate_transfer = no_certificate_transfer
        self.single_bucket_target = single_bucket_target
        # Experiment-card ablations (10-30): no_fingerprint_grouping removes
        # the arrival signature-index lookup (every arrival is decided against
        # the existing class representatives); no_pool_reuse starts every pair
        # decision from the seed pool, so no decision consults a library that
        # another pair installed.
        self.no_fingerprint_grouping = no_fingerprint_grouping
        self.no_pool_reuse = no_pool_reuse
        self._in_fresh_pool = False
        self._special_arrival = False
        self._quick_pair_origin = {}
        self._arrival_requested = set()
        self._lib_overlap_pair = {}
        self._lib_overlap_count = 0
        self._quick_after_add = None
        self.quick_calls_arrival = 0
        self._rep_depth_caps = {}
        self._global_depth_cap = None
        self._depth_failures = 0
        self._rep_deep_failures = {}
        self._deferred_deepening = []
        self.random_representative = random_representative
        self._rep_rng = random.Random(seed)
        self.metrics = dict(solver_calls=0, prover_calls=0, small_db_executions=0,
                            rejected_candidates=0, invalid_admitted_certificates=0,
                            retries=0, fingerprint_misses=0, pair_decision_requests=0,
                            prover_skipped_inadmissible=0,
                            depth_caps_learned=0, depth_caps_applied=0,
                            global_depth_caps_learned=0, global_depth_caps_applied=0,
                            deep_searches_skipped=0,
                            deferred_deepening_calls=0, deferred_deepening_upgrades=0,
                            finalization_reserve_s=self.finalization_reserve_s,
                            quick_witness_calls=0, quick_witness_admitted=0,
                            quick_witness_slow_calls=0,
                            quick_witness_skipped_after_failures=0,
                            quick_witness_rejected=0,
                            quick_separated_pairs=0, quick_separation_checked=0,
                            quick_arrival_interval_separated_pairs=0,
                            fingerprint_separated_pairs=0, polygon_separated_pairs=0,
                            polygon_candidate_search_calls=0, polygon_bounded_search_calls=0,
                            arrival_pool_hits=0, arrival_pool_hits_separated=0,
                            tail_candidate_calls=0,
                            late_evidence_rejected=0, late_arrivals_rejected=0,
                            first_fingerprint_separated_pairs=0,
                            cross_pair_reuse=0, pool_resets=0,
                            decision_pool_size=0,
                            inherited_labels=0, transferred_labels=0,
                            augment_rounds=0, augment_extra_pairs=0,
                            encoding_s=0., pool_execution_s=0., refutation_search_s=0.,
                            bounded_proof_s=0., replay_s=0.)
        self._last_split = None
        self._install({t: [] for t in schema.tables}, seed=True, origin='seed')
        if random_dbs:
            from .fast import FastGen
            gen = FastGen(schema, [], dialect, seed=seed)
            for _ in range(random_dbs):
                self._install(gen.next(), seed=True, origin='seed')

    @staticmethod
    def _cpu():
        a, b = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
        return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime

    def remaining(self):
        return max(0., self.deadline - time.perf_counter())

    def _count(self):
        self.metrics['small_db_executions'] += 1

    def _run(self, db, qids, phase='pool_execution_s'):
        started = time.perf_counter()
        try:
            return self._run_impl(db, qids)
        finally:
            elapsed = time.perf_counter() - started
            self.metrics[phase] += elapsed
            if os.environ.get('SEER_CALL_TIMING'):
                self.events.append({'event': 'engine_run', 'phase': phase,
                                    'query_count': len(qids), 'duration_s': elapsed,
                                    'wall_s': time.perf_counter() - self.started,
                                    'rows': sum(len(rows) for rows in db.values())})

    def _run_impl(self, db, qids):
        inst = self.backend.open(db)
        try:
            observations = {q: observe(inst, self.prepared[q], self._count) for q in qids}
        finally:
            inst.close()
        sensitive = []
        for q in qids:
            p = self.prepared[q]
            r = self.search.irs.get(q)
            if (p.limited or (p.tree is not None and any(isinstance(x, (exp.Window, exp.GroupConcat))
                                                      for x in p.tree.walk()))
                    or (r is not None and any(getattr(a, 'func', None) == 'any_value'
                        for x in ir.walk_rel(r) if x.op == 'agg' for a in x.args[2]))):
                sensitive.append(q)
        if sensitive:
            # Deferred-FK backends accept this permutation; an engine rejecting
            # it leaves those observations unresolved, never a false witness.
            from seer2.backends import EngineRejected
            try:
                other = self.backend.open({t: list(reversed(rows)) for t, rows in db.items()})
            except EngineRejected:
                for q in sensitive:
                    observations[q].stable = False
                    observations[q].reason = 'insertion_order_probe_rejected'
                    observations[q].cardinality = None
            else:
                try:
                    for q in sensitive:
                        check = observe(other, self.prepared[q], self._count)
                        if check.cardinality != observations[q].cardinality:
                            observations[q].cardinality = None
                        if observations[q].stable and (not check.stable or check.key != observations[q].key):
                            observations[q].stable = False
                            observations[q].reason = 'insertion_order_sensitive'
                finally:
                    other.close()
        return observations

    def _domain_covers(self, db):
        from .funcs import day_of
        dom = self.search.domain
        for t, rows in db.items():
            for row in rows:
                for c in self.schema.tables[t].columns:
                    v = row[c.name]
                    if v is None:
                        continue
                    if c.type == 'str' and v not in dom.index:
                        return False
                    if c.type == 'int' and (not isinstance(v, int) or not -(2**31) <= v < 2**31):
                        return False
                    if c.type == 'date':
                        try:
                            native_date = v.date() if isinstance(v, dt.datetime) else (
                                v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)))
                            if not dom.dlo <= day_of(native_date) <= dom.dhi:
                                return False
                        except (ValueError, TypeError):
                            return False
                    if c.type in ('float', 'ts'):
                        # No machine-floating/temporal representation theorem
                        # has been admitted for inheritance yet.
                        return False
                    if c.type == 'dec':
                        prec, scale = c.param or (18, 2)
                        scaled = decimal.Decimal(v) * (10**scale)
                        if scaled != scaled.to_integral_value() or abs(scaled) >= 10**min(prec, 18):
                            return False
        return True

    def _covered(self, edge, witness):
        return edge.status == 'proved' or (edge.domain == witness.domain and
                edge.domain == self.search.domain_identity() and witness.k <= edge.k
                and self._domain_covers(witness.database))

    def _path(self, a, b, witness=None, direct_only=None):
        if a == b:
            return []
        if direct_only is None:
            direct_only = self.no_certificate_transfer
        seen = {a}; todo = deque([(a, [])])
        while todo:
            q, path = todo.popleft()
            if direct_only and len(path) >= 1:
                continue
            for edge in self.adjacency.get(q, ()):
                if edge.status == 'bounded' and edge.domain != self.search.domain_identity():
                    continue
                if witness is not None and not self._covered(edge, witness):
                    continue
                nxt = edge.b if edge.a == q else edge.a if edge.b == q else None
                if nxt is None or nxt in seen:
                    continue
                # Edges cannot join members with conflicting observed results.
                if self.fingerprints.get(a) != self.fingerprints.get(nxt):
                    continue
                extended = path + [edge]
                if nxt == b:
                    return extended
                seen.add(nxt); todo.append((nxt, extended))
        return None

    def relation(self, q, representative=None):
        representative = representative or self.representatives[self.fingerprints[q]]
        path = self._path(q, representative)
        if q == representative:
            return {'status': 'representative'}
        if path is None:
            status, reason = self.pending_reason.get(q, ('undecided', 'no_admitted_evidence_path'))
            return {'status': status, 'reason': reason}
        bounded = [e for e in path if e.status == 'bounded']
        return {'status': 'bounded' if bounded else 'proved',
                'k': min(e.k for e in bounded) if bounded else None,
                'domain': bounded[0].domain if bounded else None,
                'receipts': [e.receipt for e in path]}

    @staticmethod
    def _wkey(w, q):
        o = w.observations[q]
        return o.cardinality if w.mode == 'cardinality' else o.key

    @staticmethod
    def _wmode(w):
        if all(o.stable for o in w.observations.values()):
            w.active, w.mode = True, 'full'
        elif all(o.cardinality is not None for o in w.observations.values()):
            w.active, w.mode = True, 'cardinality'
        else:
            w.active, w.mode = False, 'unresolved'
        w.reason = next((o.reason for o in w.observations.values() if not o.stable), '')

    def _reindex(self):
        old = dict(self.representatives)
        self.fingerprints = {q: tuple(self._wkey(w, q) for w in self.witnesses if w.active)
                             for q in self.arrival}
        self.index = {}
        for q in self.arrival:
            self.index.setdefault(self.fingerprints[q], []).append(q)
        self.representatives = {}
        for fp, members in self.index.items():
            rep = old.get(fp, members[0])
            if rep not in members:
                rep = members[0]
            reachable = self._component(rep)
            eligible = [q for q in members if q in reachable]
            simpler = (self._rep_rng.choice(sorted(eligible)) if self.random_representative else
                       min(eligible, key=lambda q: (self.complexity[q], self.arrival.index(q))))
            self.representatives[fp] = simpler
            if simpler != rep:
                self.events.append({'event': 'representative_replaced', 'old': rep, 'new': simpler})

    def _coverage_event(self, kind, **fields):
        n = len(self.arrival)
        total = n * (n-1) // 2
        within = sum(len(m) * (len(m)-1) // 2 for m in self.index.values())
        self.events.append({'event': kind, 'wall_s': time.perf_counter()-self.started,
                            'processed_queries': n, 'total_pairs': total,
                            'separated_pairs': total-within,
                            'class_count': len(self.index), **fields})

    SEPARATION_CATEGORIES = ('quick', 'fingerprint', 'polygon')

    def _column_origin(self, w):
        """Replay category of a column: seed columns are the base pool and
        contribute no library category; decision libraries (quick, polygon)
        and origin-less pool installs ('fingerprint') keep their own."""
        origin = self._witness_origin.get(id(w))
        if origin is None or origin == 'seed':
            return None
        return origin if origin in self.SEPARATION_CATEGORIES else 'polygon'

    def seed_base_separated_pairs(self):
        """Pairs the seed base column (first active column) separates. These
        are the pairs counted live under the 'fingerprint' category when the
        first decision library arrives (its gain re-splits every pair that the
        base column alone separated); they are reported separately."""
        active = [w for w in self.witnesses if w.active]
        if len(active) < 1:
            return 0
        base = active[0]
        if self._column_origin(base) is not None:
            # No seed base at all (unreachable in practice): nothing to count.
            return 0
        return sum(1 for i, a in enumerate(self.arrival)
                   for b in self.arrival[i + 1:]
                   if self._wkey(base, a) != self._wkey(base, b))

    def _count_until(self, key, length=None):
        limit = len(self.events) if length is None else length
        return sum(1 for e in self.events[limit:] if e.get('event') == key)

    def _attribute_split(self, w, origin, before=None):
        """Attribute one accepted library's separations.

        new  = pairs this library is the FIRST to separate (its own gain)
        overlap = pairs it separates that an earlier column already separated
        (the arrival fingerprint or an earlier library); these are the raw
        pool-reuse events and are counted for every library, not once.
        """
        old_pairs = []
        separated = 0
        new_origin = self._column_origin(w) if w is not None else None
        if before is not None and new_origin is not None and w.active:
            # One immutable pre-install partition for BOTH counts. A library
            # can distinguish pairs already split by any earlier class/library.
            # Such pairs are overlap, not a second deduction from net gain.
            queries = tuple(self.arrival)
            assert set(before) == set(queries), 'split snapshot misses arrived queries'
            keys = {q: self._wkey(w, q) for q in queries}
            for i, a in enumerate(queries):
                for b in queries[i + 1:]:
                    if keys[a] == keys[b]:
                        continue
                    separated += 1
                    if before[a] != before[b]:
                        old_pairs.append(frozenset((a, b)))
            sizes_before = Counter(before.values())
            sizes_after = Counter((before[q], keys[q]) for q in queries)
            partition_gain = (sum(c*(c-1)//2 for c in sizes_before.values())
                              - sum(c*(c-1)//2 for c in sizes_after.values()))
            assert separated - len(old_pairs) == partition_gain, \
                'library gain differs from the same-snapshot partition delta'
        overlap = len(old_pairs)
        gain = separated - overlap
        assert gain >= 0, 'batch library gain below its overlap'
        assert gain + overlap == separated, 'library pair accounting is not exhaustive'
        key = origin if origin in self.SEPARATION_CATEGORIES else 'polygon'
        self.metrics[key + '_separated_pairs'] += gain
        if key == 'polygon':
            self.metrics['polygon_library_separated_pairs'] = \
                self.metrics.get('polygon_library_separated_pairs', 0) + gain
        for pair in old_pairs:
            self._lib_overlap_pair.setdefault(pair, new_origin)
        self._lib_overlap_count += overlap
        self._last_split = (key, gain, overlap, id(w) if w is not None else None,
                            list(old_pairs))

    def _reverse_split(self):
        if self._last_split is None:
            return
        key, gain, overlap, wid, old_pairs = self._last_split
        self.metrics[key + '_separated_pairs'] -= gain
        if key == 'polygon':
            self.metrics['polygon_library_separated_pairs'] -= gain
        if wid is not None:
            self._witness_origin.pop(wid, None)
        self._lib_overlap_count -= overlap
        for pair in old_pairs:
            self._lib_overlap_pair.pop(pair, None)
        self._last_split = None

    def final_split_attribution(self):
        """Attribute each pair in the final partition to exactly one source.

        The install-time counters are event counts: they include a separation
        recorded at arrival even if the column that produced it is later
        deactivated, and they can include a library separation that a later
        deactivation merges. The final partition is the authority. For every
        pair that is still separated, the earliest active column that
        distinguishes it decides the source: a column present when the later
        query arrived is an arrival-fingerprint separation; a decision library
        installed afterwards is credited to that library. A pair distinguished
        by neither would be a search separation. This is state-based and
        therefore counts each pair once, without overlapping categories.

        Under no_fingerprint_grouping the arrival path credits no separation,
        so seed-column separations are reported as the seed pool instead.
        """
        cats = dict.fromkeys(
            self.SEPARATION_CATEGORIES + ('arrival', 'seed', 'search'), 0)
        active = [w for w in self.witnesses if w.active]
        if len(self.arrival) < 2 or not active:
            return cats
        keys = {q: tuple(self._wkey(w, q) for w in active) for q in self.arrival}
        installs = [getattr(w, 'arrival_at_install', 0) for w in active]
        origins = [self._column_origin(w) for w in active]
        separated = 0
        for i, a in enumerate(self.arrival):
            ka = keys[a]
            for j in range(i + 1, len(self.arrival)):
                b = self.arrival[j]
                kb = keys[b]
                k = 0
                while k < len(active) and ka[k] == kb[k]:
                    k += 1
                if k == len(active):
                    continue
                separated += 1
                origin = origins[k]
                if self.no_fingerprint_grouping:
                    # Index-off still retains valid libraries as output
                    # evidence, but no later arrival bypasses a decision via
                    # index lookup. Attribute to the generating library even
                    # when that library predates the later query.
                    if origin is None:
                        cats['seed'] += 1
                    elif origin in ('quick', 'polygon'):
                        cats[origin] += 1
                    # Origin-less decision columns have no index attribution;
                    # the remainder below records them as search evidence.
                elif installs[k] <= j:
                    cats['arrival'] += 1
                else:
                    cats[origin if origin in self.SEPARATION_CATEGORIES
                         else 'polygon'] += 1
        attributed = cats['arrival'] + cats['seed'] + sum(
            cats[key] for key in self.SEPARATION_CATEGORIES)
        cats['search'] = separated - attributed
        assert cats['search'] >= 0, 'more attributed pairs than separated pairs'
        return cats

    def _raw_library_overlaps(self):
        """Recompute raw pool-reuse events from the stored observations.

        Only the queries that had already arrived when a library was installed
        can be separated by it; a later arrival is counted at its own arrival.
        Returns (overlap, pair_map): overlap = pairs a library separates that
        an earlier active column already separated, summed over libraries;
        pair_map maps such a pair to the first library that re-separated it.
        This is an event count, not a partition attribution: duplicates across
        libraries are intentional and reconciled with the install-time log.
        """
        overlap = 0
        pair_map = {}
        active = [w for w in self.witnesses if w.active]
        if len(active) < 2:
            return overlap, pair_map
        for idx in range(1, len(active)):
            origin = self._column_origin(active[idx])
            if origin is None:
                continue
            seen = getattr(active[idx], 'arrival_at_install', len(self.arrival))
            qs = self.arrival[:seen]
            keys = [{k: self._wkey(w, q) for k, w in enumerate(active)} for q in qs]
            old = 0
            for i in range(len(qs)):
                for j in range(i + 1, len(qs)):
                    if keys[i][idx] == keys[j][idx]:
                        continue
                    if any(keys[i][k] != keys[j][k] for k in range(idx)):
                        old += 1
                        pair_map.setdefault((qs[i], qs[j]), origin)
            overlap += old
        return overlap, pair_map

    def raw_split_events(self):
        """Return the state-based partition categories plus raw overlap events.

        Categories are recomputed from the final active columns by
        final_split_attribution(), so each final separated pair appears once.
        Overlap remains the raw per-library event count used to measure pool
        reuse, which deliberately counts a pair again for every library that
        re-separates it.
        """
        full = self.final_split_attribution()
        cats = {key: full[key] for key in self.SEPARATION_CATEGORIES}
        overlap, pair_map = self._raw_library_overlaps()
        return cats, overlap, pair_map

    def _quick_board(self):
        # Pairs that quick steps may address before any proof, in the order
        # the arrival stage compares them: each class representative against
        # all later arrivals of its class, and each class's later arrivals
        # pair by pair. A pair is taken off the board once a library or a
        # decision separates it.
        reps = dict(self.representatives)
        board = []
        for fp in dict.fromkeys(self.fingerprints[q] for q in self.arrival):
            members = self.index[fp]
            if len(members) < 2:
                continue
            rep_q = reps[fp]
            for b in members:
                if b != rep_q:
                    board.append((rep_q, b))
            rest = [m for m in members if m != rep_q]
            for i, a in enumerate(rest):
                for b in rest[i + 1:]:
                    board.append((a, b))
        return board

    def _split(self, q, x):
        if q not in self.fingerprints or x not in self.fingerprints:
            return False
        if self.fingerprints[q] != self.fingerprints[x]:
            return True
        return any(w.active and self._wkey(w, q) != self._wkey(w, x) for w in self.witnesses)

    def _compare_arrival_quick(self, q):
        """Official SQLWitness quick step for one arrival, before any proof.

        The quick step only proposes libraries. Each library passes the same
        Sigma check, fresh replay of every required query and two independent
        target replays as polygon proposals; a proposal that fails admission
        establishes nothing. Returns whether an accepted library arrived.
        """
        rep = self.representatives[self.fingerprints[q]]
        if self.quick_witness is None or rep == q or time.perf_counter() >= self.deadline:
            return False
        self._quick_ran.add(frozenset((q, rep)))
        self._quick_query_pairs.add((q, rep))
        self._quick_pair_origin.setdefault((q, rep), 'arrival')
        self.metrics['quick_witness_calls'] += 1
        phase_start = time.perf_counter()
        try:
            quick = self.quick_witness(
                self.queries[q], self.queries[rep],
                max(0., min(self.per_call_s, self.deadline - time.perf_counter())))
        finally:
            self.metrics['refutation_search_s'] += time.perf_counter()-phase_start
        self.metrics['small_db_executions'] += int(
            quick.get('nested_small_db_executions') or 0)
        record = {k: v for k, v in quick.items() if k != 'database'}
        native = quick.get('native') or {}
        wall = float(quick.get('wall') or native.get('wall') or 0.)
        native_s = float(native.get('tool_secs') or native.get('time_s') or 0.)
        # Supervision assertion (10-29): a quick call must return on the same
        # scale as the released per-pair driver; anomalous calls are counted
        # and the runner asserts the count stays zero.
        if wall > max(5.0, 10.0 * native_s):
            self.metrics['quick_witness_slow_calls'] += 1
        record.update(event='quick_witness', query=q, representative=rep,
                      fingerprint=self.fingerprints[q], stage='arrival',
                      wall_s=wall, native_s=native_s)
        self.events.append(record)
        if quick.get('database') is None:
            self.quick_failure_streak[rep] = self.quick_failure_streak.get(rep, 0) + 1
            return False
        if self._install(quick['database'], target=(q, rep), origin='quick'):
            self.quick_failure_streak[rep] = 0
            self.metrics['quick_witness_admitted'] += 1
            record['engine_admitted'] = True
            self._quick_admitted[(q, rep)] = True
            self._quick_admission_columns[(q, rep)] = self.witnesses[-1]
            return True
        self.quick_failure_streak[rep] = self.quick_failure_streak.get(rep, 0) + 1
        record['engine_admitted'] = False
        self.metrics['quick_witness_rejected'] += 1
        return False

    def _save_replay_context(self, reason, **details):
        """Preserve typed values and full SQL/receipt context before any repair."""
        root = Path(getattr(self, 'evidence_dir', None) or
                    os.environ.get('SEER_REPLAY_EVIDENCE_DIR') or
                    os.environ.get('TMPDIR') or tempfile.gettempdir()) / 'replay-failures'
        root.mkdir(parents=True, exist_ok=True)
        context = dict(reason=reason, schema=self.schema, queries=self.queries,
                       arrival=self.arrival, witnesses=self.witnesses, edges=self.edges,
                       fingerprints=self.fingerprints, representatives=self.representatives,
                       pending_reason=self.pending_reason, events=self.events,
                       domain=self.search.domain, domain_sha256=self.search.domain_identity(),
                       prepared_sql={q: p.sql for q, p in self.prepared.items()}, **details)
        with tempfile.NamedTemporaryFile(prefix='context-', suffix='.pickle', dir=root,
                                         delete=False) as handle:
            pickle.dump(context, handle, protocol=pickle.HIGHEST_PROTOCOL)
            path = handle.name
        Path(path + '.json').write_text(json.dumps(
            {'reason': reason, 'context': path, **details}, default=repr, indent=2))
        return path

    def _verified_observations(self, db, required, inherited):
        observations = self._run(db, required)
        actual = self._run(db, list(inherited), phase='replay_s') if inherited else {}
        for q, rep in inherited.items():
            expected = observations[rep]
            if actual[q] != expected:
                path = self._save_replay_context('inheritance_replay_mismatch',
                    database=db, query=q, representative=rep, expected=expected, actual=actual[q])
                self.events.append({'event': 'inheritance_replay_mismatch', 'query': q,
                                    'representative': rep, 'context': path})
                self.metrics['inheritance_replay_mismatches'] = self.metrics.get(
                    'inheritance_replay_mismatches', 0) + 1
                if actual[q].stable and expected.stable and actual[q].key != expected.key:
                    witness = Witness(db, self.search.domain_identity(),
                                      max(map(len, db.values()), default=0))
                    contradicted = self._path(q, rep, witness) or []
                    # The independently executed database refutes the path.
                    # Its individual faulty edge is not known, so revoke all
                    # edges of that path and retain their original receipts in
                    # the saved context. Never export a contradicted proof.
                    revoked = {id(edge) for edge in contradicted}
                    self.edges = [edge for edge in self.edges if id(edge) not in revoked]
                    for node in self.adjacency:
                        self.adjacency[node] = [edge for edge in self.adjacency[node]
                                                if id(edge) not in revoked]
                    self.events.append({'event': 'certificate_path_revoked', 'query': q,
                                        'representative': rep, 'edges': len(revoked), 'context': path})
                    self.metrics['revoked_certificate_edges'] = self.metrics.get(
                        'revoked_certificate_edges', 0) + len(revoked)
            else:
                self.metrics['inherited_labels'] += 1
            # Always keep the actual member observation (including stability
            # and cardinality); never alias a representative's mutable object.
            observations[q] = actual[q]
            assert observations[q] == actual[q], 'unverified inherited observation'
        return observations

    def _install(self, db, target=None, seed=False, admission_deadline=None, origin=None):
        cutoff = min(self.deadline, admission_deadline or self.deadline)
        if time.perf_counter() >= cutoff:
            self.metrics['late_evidence_rejected'] += 1
            self.events.append({'event': 'late_witness_rejected',
                                'wall_s': time.perf_counter()-self.started,
                                'target': target})
            return False
        if self.schema.violations(db):
            self.metrics['rejected_candidates'] += 1
            return False
        db = copy.deepcopy(db)
        w = Witness(db, self.search.domain_identity(), max(map(len, db.values()), default=0))
        # Replay every representative and every unresolved member. Followers
        # inherit only through edges whose complete scope covers this witness.
        required = []
        inherited = {}
        for q in self.arrival:
            rep = self.representatives.get(self.fingerprints.get(q), q)
            path = None if self.no_certificate_transfer else self._path(q, rep, w)
            if q != rep and path is not None:
                inherited[q] = rep
            else:
                required.append(q)
        from seer2.backends import EngineRejected
        try:
            observations = self._verified_observations(db, required, inherited)
        except EngineRejected:
            self.metrics['rejected_candidates'] += 1
            return False
        w.observations = observations
        self._wmode(w)
        if target is not None:
            a, b = target
            # Target must be independently executed in two fresh DB instances.
            checks = self._run(db, [a, b], phase='replay_s')
            check_w = Witness(db, w.domain, w.k, checks, mode=w.mode)
            if (not w.active or (w.mode == 'full' and not all(o.stable for o in checks.values())) or
                    any(checks[q].cardinality is None for q in (a,b)) or
                    self._wkey(check_w, a) == self._wkey(check_w, b) or
                    any(self._wkey(check_w, q) != self._wkey(w, q) for q in (a, b))):
                self.metrics['rejected_candidates'] += 1
                return False
        if time.perf_counter() >= cutoff:
            self.metrics['late_evidence_rejected'] += 1
            self.events.append({'event': 'late_witness_rejected',
                                'wall_s': time.perf_counter()-self.started,
                                'target': target})
            return False
        previous = (self.fingerprints, self.index, self.representatives,
                    self._rep_rng.getstate(), len(self.events))
        pre_fp = dict(self.fingerprints)
        # Record the attribution category of every column: seed bases are not
        # libraries, origin-less installs count as pool ('fingerprint')
        # columns, matching the live category counters.
        if origin is not None:
            self._witness_origin[id(w)] = origin
        elif not seed:
            self._witness_origin[id(w)] = 'fingerprint'
        w.arrival_at_install = len(self.arrival)
        self.witnesses.append(w)
        self._reindex()
        self._attribute_split(w, origin, before=pre_fp)
        self._coverage_event('pool_added', size=len(self.witnesses), active=w.active,
                             origin=origin)
        if time.perf_counter() >= cutoff:
            self.witnesses.pop()
            self.fingerprints, self.index, self.representatives = previous[:3]
            self._reverse_split()
            self._rep_rng.setstate(previous[3])
            del self.events[previous[4]:]
            self.metrics['late_evidence_rejected'] += 1
            self.events.append({'event': 'late_witness_rejected',
                                'wall_s': time.perf_counter()-self.started,
                                'target': target})
            return False
        return True

    def install_batch(self, candidates):
        candidates = list(candidates)
        try:
            return self._install_batch(candidates)
        except AssertionError as exc:
            self._save_replay_context('batch_install_assertion',
                                      candidates=candidates, exception=repr(exc))
            raise

    def _install_batch(self, candidates):
        """Validate native candidates, then add their columns and split classes once."""
        accepted = []
        seen = set()
        from seer2.backends import EngineRejected
        for db, target in candidates:
            if self.remaining() <= self.finalization_reserve_s:
                break
            stamp = time.perf_counter()
            invalid = self.schema.violations(db)
            self.metrics['sigma_validation_s'] = self.metrics.get('sigma_validation_s', 0.) + time.perf_counter() - stamp
            if invalid:
                self.metrics['rejected_candidates'] += 1
                continue
            fingerprint = repr(db)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            a, b = target
            if self.fingerprints[a] != self.fingerprints[b]:
                continue
            if any(self._wkey(w, a) != self._wkey(w, b) for w in accepted if w.active):
                continue
            db = copy.deepcopy(db)
            w = Witness(db, self.search.domain_identity(), max(map(len, db.values()), default=0))
            try:
                # Bounded/proved followers inherit the representative result only
                # when the receipt covers this witness's row bound and domain.
                required = []
                inherited = {}
                for q in self.arrival:
                    rep = self.representatives.get(self.fingerprints.get(q), q)
                    path = None if self.no_certificate_transfer else self._path(q, rep, w)
                    if q != rep and path is not None:
                        inherited[q] = rep
                    else:
                        required.append(q)
                observations = self._verified_observations(db, required, inherited)
                # Candidate admission still independently replays both targets.
                checks = self._run(db, [a, b], phase='replay_s')
            except EngineRejected:
                self.metrics['rejected_candidates'] += 1
                continue
            w.observations = observations
            self._wmode(w)
            check_w = Witness(db, w.domain, w.k, checks, mode=w.mode)
            if (not w.active or (w.mode == 'full' and not all(o.stable for o in checks.values()))
                    or any(checks[q].cardinality is None for q in (a, b))
                    or self._wkey(check_w, a) == self._wkey(check_w, b)
                    or any(self._wkey(check_w, q) != self._wkey(w, q) for q in (a, b))):
                self.metrics['rejected_candidates'] += 1
                continue
            accepted.append(w)
        if accepted and self.remaining() > 0:
            stamp = time.perf_counter()
            # Batch installs add several libraries at once. Replay them one at
            # a time on the stored observations to keep the same attribution
            # as sequential installs: each library's gain is the pairs it is
            # the first to separate, and its overlap events are the pairs it
            # separates that an earlier active column already separated.
            pre = dict(self.fingerprints)
            for w in accepted:
                self._witness_origin[id(w)] = 'quick'
                w.arrival_at_install = len(self.arrival)
            for w in accepted:
                if not w.active:
                    continue
                # Use exactly the sequential-install accounting. Advance the
                # snapshot after each accepted column so later columns in this
                # batch see the earlier columns' separations as overlap.
                self._attribute_split(w, 'quick', before=pre)
                pre = {q: pre[q] + (self._wkey(w, q),) for q in self.arrival}
            self.witnesses.extend(accepted)
            self._reindex()
            self._last_split = None
            self.metrics['m5_reindex_s'] = self.metrics.get('m5_reindex_s', 0.) + time.perf_counter() - stamp
            self._coverage_event('pool_added_batch', count=len(accepted), size=len(self.witnesses))
            self.metrics['quick_witness_admitted'] += len(accepted)

        return len(accepted)

    def add(self, q, sql, compare=True):
        if not isinstance(q, str) or q in self.queries:
            raise ValueError('query IDs must be unique strings')
        if self.remaining() <= 0:
            self.metrics['late_arrivals_rejected'] += 1
            self.events.append({'event': 'late_arrival_rejected',
                                'wall_s': time.perf_counter()-self.started, 'query': q})
            return False

        # Encoding and pool execution can finish after the batch deadline.
        # Roll back the entire arrival if its index update misses the cutoff.
        search = self.search
        prior = {
            'search_queries': dict(search.queries),
            'irs': dict(search.irs),
            'unsupported': dict(search.unsupported),
            'ir_order': list(search.ir_order),
            'domain_inputs': copy.deepcopy(search.domain_inputs),
            'domain': search.domain,
            'domain_revision': search.domain_revision,
            'levels': dict(search.levels),
            'trace_len': len(search.trace),
            'arrival_len': len(self.arrival),
            'fingerprints': dict(self.fingerprints),
            'index': {fp: list(members) for fp, members in self.index.items()},
            'representatives': dict(self.representatives),
            'rng': self._rep_rng.getstate(),
            'events_len': len(self.events),
            'witness_modes': [(w, w.active, w.mode) for w in self.witnesses],
            'first_sep': self.metrics['first_fingerprint_separated_pairs'],
            'fingerprint_misses': self.metrics['fingerprint_misses'],
        }

        def reject_late():
            search.queries = prior['search_queries']
            search.irs = prior['irs']
            search.unsupported = prior['unsupported']
            search.ir_order = prior['ir_order']
            search.domain_inputs = prior['domain_inputs']
            search.domain = prior['domain']
            search.domain_revision = prior['domain_revision']
            search.levels = prior['levels']
            del search.trace[prior['trace_len']:]
            self.queries.pop(q, None)
            self.prepared.pop(q, None)
            self.complexity.pop(q, None)
            del self.arrival[prior['arrival_len']:]
            self.fingerprints = prior['fingerprints']
            self.index = prior['index']
            self.representatives = prior['representatives']
            self._rep_rng.setstate(prior['rng'])
            del self.events[prior['events_len']:]
            for w, active, mode in prior['witness_modes']:
                w.observations.pop(q, None)
                w.active, w.mode = active, mode
            self.metrics['first_fingerprint_separated_pairs'] = prior['first_sep']
            self.metrics['fingerprint_misses'] = prior['fingerprint_misses']
            self.metrics['late_arrivals_rejected'] += 1
            self.events.append({'event': 'late_arrival_rejected',
                                'wall_s': time.perf_counter()-self.started, 'query': q})
            return False

        self.queries[q] = sql
        started = time.perf_counter()
        try:
            self.prepared[q] = Prepared(sql, self.dialect, self.backend.dialect)
            tree = self.prepared[q].tree
            self.complexity[q] = sum(1 for _ in tree.walk()) if tree is not None else len(sql)
            old_domain = search.domain_identity()
            search.add_query(q, sql)
        finally:
            self.metrics['encoding_s'] += time.perf_counter() - started
        if self.remaining() <= 0:
            return reject_late()
        if search.domain_identity() != old_domain:
            self.events.append({'event': 'bounded_scope_invalidated', 'query': q})
        self.arrival.append(q)
        prior_reps = list(dict.fromkeys(self.representatives.values()))
        prior_members = []
        disabled_column = False
        # A new unstable query disables that column for the entire partition,
        # preserving a single transitive key; it is never a wildcard per pair.
        for w in self.witnesses:
            o = self._run(w.database, [q])[q]
            w.observations[q] = o
            old_mode = (w.active, w.mode)
            self._wmode(w)
            disabled_column |= old_mode != (w.active, w.mode)
            if self.remaining() <= 0:
                return reject_late()
        if disabled_column:
            self._reindex()
        else:
            fp = tuple(self._wkey(w, q) for w in self.witnesses if w.active)
            prior_members = list(self.index.get(fp, []))
            self.fingerprints[q] = fp
            self.index.setdefault(fp, []).append(q)
            self.representatives.setdefault(fp, q)
        if self.remaining() <= 0:
            return reject_late()
        members = self.index[self.fingerprints[q]]
        separated_delta = len(self.arrival) - len(members)
        self.metrics['arrival_pool_hits'] += 1
        if not self.no_fingerprint_grouping:
            # With fingerprint grouping ablated the arrival performs no
            # signature-index lookup, so it cannot credit a pool separation.
            self.metrics['arrival_pool_hits_separated'] += separated_delta
            self.metrics['first_fingerprint_separated_pairs'] += separated_delta
        self._coverage_event('arrival', query=q)
        if self.remaining() <= 0:
            return reject_late()
        if disabled_column:
            # A column that becomes unresolved or cardinality-only no longer
            # certifies all of its original full-value separations. The
            # partition above already uses the new keys; keep the live quick
            # admission ledger in the same state. Historical acceptance events
            # and counters remain intact, and the final safety assertions stay.
            self._refresh_quick_admissions()
        self._special_arrival = False
        if getattr(self, '_class_arrival_only', False):
            from .class_ablation import queue_arrival_pairs
            if queue_arrival_pairs(self, q, prior_reps, prior_members):
                self._special_arrival = True
                return True
        if self.no_fingerprint_grouping:
            # Every arrival enters the representative-decision path against
            # the representatives that existed before it. A cross-class pair is
            # already separated by an active library, so only a refutation
            # decision is meaningful; _compare enforces that.
            self._special_arrival = True
            self._arrival_requested.add(q)
            saw = set()
            for rep in prior_reps:
                if rep == q or rep in saw:
                    continue
                saw.add(rep)
                self._compare(q, rep=rep, force=True,
                              refutation_only=not compare, cross_class=True)
            return True
        if self.no_certificate_transfer:
            # No in-class transitivity: decide the arrival against every
            # member that already shared its class, one pair at a time.
            self._special_arrival = True
            self._arrival_requested.add(q)
            candidates = prior_members or [m for m in self.index[self.fingerprints[q]]
                                           if m != q]
            saw = set()
            for other in candidates:
                if other == q or other in saw:
                    continue
                saw.add(other)
                self._compare(q, rep=other, force=True,
                              refutation_only=not compare)
            return True
        if len(members) == 1:
            self.metrics['fingerprint_misses'] += 1
            return True
        if self._quick_after_add is not None:
            self._arrival_requested.add(q)
            events_before = len(self.events)
            before = len(self.index)
            separated = self._quick_after_add(q, before)
            self.quick_calls_arrival += self._count_until('quick_witness', events_before)
            if separated:
                self.metrics['quick_arrival_interval_separated_pairs'] += 1
        if compare:
            self._compare(q)
        return True

    def _compare(self, q, rep=None, force=False, deepen=False,
                 refutation_only=False, use_quick=True,
                 candidate_only=False, search_budget=None, cross_class=False):
        if self.no_pool_reuse and not self._in_fresh_pool:
            return self._compare_in_fresh_pool(
                q, rep, force=force, deepen=deepen,
                refutation_only=refutation_only, use_quick=use_quick,
                candidate_only=candidate_only, search_budget=search_budget,
                cross_class=cross_class)
        rep = self.representatives[self.fingerprints[q]] if rep is None else rep
        if self.fingerprints[q] != self.fingerprints[rep]:
            if not cross_class:
                if not self.no_pool_reuse:
                    self.metrics['cross_pair_reuse'] += 1
                return
            # The pair is already separated by an active library. There is no
            # equivalence claim to make across classes; only a refutation
            # decision is meaningful.
            refutation_only = True
        if q == rep:
            return
        if (not cross_class and not force and not self.no_certificate_transfer
                and self._path(q, rep) is not None):
            if not self.no_pool_reuse:
                self.metrics['cross_pair_reuse'] += 1
            return
        if self.remaining() <= 0:
            self.pending_reason[q] = ('undecided', 'batch_budget')
            return
        self.metrics['pair_decision_requests'] += 1
        native = None
        call_budget = self.per_call_s if search_budget is None else search_budget + 3.0
        call_deadline = min(self.deadline - (self.finalization_reserve_s if deepen or candidate_only else 0.),
                            time.perf_counter() + call_budget)
        native_admissible = (not refutation_only and not candidate_only and not cross_class
                             and self.prover is not None and
                             (not hasattr(self.prover, 'can_admit') or
                              self.prover.can_admit(self.queries[q], self.queries[rep])))
        if self.prover is not None and not native_admissible and not refutation_only and not candidate_only:
            self.metrics['prover_skipped_inadmissible'] += 1
            self.events.append({'event': 'native_prover_skipped', 'query': q,
                                'representative': rep, 'reason': 'inadmissible_semantics_profile'})
        if native_admissible:
            self.metrics['prover_calls'] += 1
            phase_start = time.perf_counter()
            try:
                native = self.prover(self.queries[q], self.queries[rep], max(0., call_deadline-time.perf_counter()))
            finally:
                self.metrics['bounded_proof_s'] += time.perf_counter()-phase_start
            self.events.append({'event': 'native_prover', 'query': q, 'representative': rep, 'receipt': native})
            if time.perf_counter() >= call_deadline:
                self.metrics['late_evidence_rejected'] += 1
                self.pending_reason[q] = ('undecided', 'pair_or_batch_deadline')
                return
            # A callback must explicitly establish comparison/dialect/Σ scope;
            # an unqualified native EQ is deliberately insufficient.
            if (native.get('status') == 'native_eq' and self.prover_admit is not None
                    and self.prover_admit(q, rep, native)):
                self._add_edge(Edge(q, rep, 'proved', native), call_deadline)
                return
        if use_quick and self.quick_witness is not None and time.perf_counter() < call_deadline:
            if (self.quick_failure_cap is not None and
                    self.quick_failure_streak.get(rep, 0) >= self.quick_failure_cap):
                self.metrics['quick_witness_skipped_after_failures'] += 1
                self.events.append({'event': 'quick_witness_skipped', 'query': q,
                                    'representative': rep, 'reason': 'failure_cap'})
            else:
                self._quick_ran.add(frozenset((q, rep)))
                self._quick_query_pairs.add((q, rep))
                self._quick_pair_origin.setdefault(
                    (q, rep), 'arrival' if q in self._arrival_requested else 'remainder')
                self.metrics['quick_witness_calls'] += 1
                phase_start = time.perf_counter()
                try:
                    quick = self.quick_witness(self.queries[q], self.queries[rep],
                                               max(0., call_deadline-time.perf_counter()))
                finally:
                    self.metrics['refutation_search_s'] += time.perf_counter()-phase_start
                self.metrics['small_db_executions'] += int(
                    quick.get('nested_small_db_executions') or 0)
                record = {k: v for k, v in quick.items() if k != 'database'}
                record.update(event='quick_witness', query=q, representative=rep,
                              fingerprint=self.fingerprints[q], stage='compare')
                self.events.append(record)
                if time.perf_counter() >= call_deadline:
                    self.metrics['late_evidence_rejected'] += 1
                    self.pending_reason[q] = ('undecided', 'pair_or_batch_deadline')
                    return
                if quick.get('database') is not None:
                    record['engine_admitted'] = self._install(quick['database'], target=(q, rep),
                                                             admission_deadline=call_deadline,
                                                             origin='quick')
                    if not record['engine_admitted']:
                        self.metrics['quick_witness_rejected'] += 1
                    if record['engine_admitted']:
                        self.quick_failure_streak[rep] = 0
                        self.metrics['quick_witness_admitted'] += 1
                        self._quick_admitted[(q, rep)] = True
                        self._quick_admission_columns[(q, rep)] = self.witnesses[-1]
                        self.pending_reason[q] = ('undecided', 'quick_witness_separated')
                        return
                self.quick_failure_streak[rep] = self.quick_failure_streak.get(rep, 0) + 1
        if refutation_only:
            self.pending_reason[q] = ('undecided', 'tail_proof_pending')
            return
        if deepen and (self._rep_deep_failures.get(rep, 0) >= 2 or self._depth_failures >= 2):
            self.metrics['deep_searches_skipped'] += 1
            self.events.append({'event': 'deep_search_skipped', 'query': q,
                                'representative': rep, 'reason': 'two_consecutive_deep_failures'})
            return
        reps = list(self.representatives.values())
        pairs = list(zip(reps, reps[1:]))
        search_bounds = (1,) if candidate_only else self.bounds
        rep_cap = self._rep_depth_caps.get(rep) if self.bounds is None and not deepen else None
        global_cap = self._global_depth_cap if self.bounds is None and not deepen else None
        caps = [value for value in (rep_cap, global_cap) if value is not None]
        if caps and not candidate_only:
            cap = min(caps)
            search_bounds = tuple(k for k in (1, 2, 4) if k <= cap)
            if rep_cap is not None:
                self.metrics['depth_caps_applied'] += 1
            if global_cap is not None:
                self.metrics['global_depth_caps_applied'] += 1
            self.events.append({'event': 'depth_cap_applied', 'query': q,
                                'representative': rep, 'cap': cap,
                                'source': 'global' if global_cap is not None else 'representative'})
        # Certificate eligibility is independent of refutation eligibility.
        # In particular, SQLite AVG can yield a replayable SAT counterexample
        # even though an UNSAT prefix is not admissible as a certificate.
        from .stream_numeric import bounded_numeric_admission
        numeric_key = (q, rep)
        if numeric_key not in self._numeric_preflight:
            self._numeric_preflight[numeric_key] = bounded_numeric_admission(
                self.search.irs.get(q), self.search.irs.get(rep), self.dialect, self.schema)
        numeric_preflight = self._numeric_preflight[numeric_key]
        self.events.append({'event': 'numeric_certificate_preflight', 'query': q,
                            'representative': rep, 'admission': numeric_preflight,
                            'refutation_search_preserved': True})
        phase_start = time.perf_counter()
        search_seconds = max(0., call_deadline-time.perf_counter())
        if candidate_only:
            search_seconds = min(search_seconds, search_budget)
        receipt = self.search.search(q, rep, search_seconds, bounds=search_bounds,
                                     representative_pairs=pairs, canonical_contract=True,
                                     augment=(0 if self.single_bucket_target else 3))
        self.metrics['refutation_search_s' if candidate_only or receipt['status']=='candidate' else 'bounded_proof_s'] += time.perf_counter()-phase_start
        self.metrics['augment_rounds'] += int(receipt.get('augment_rounds') or 0)
        self.metrics['augment_extra_pairs'] += int(receipt.get('augment_extra_pairs') or 0)
        if candidate_only or receipt['status'] == 'candidate':
            self.metrics['polygon_candidate_search_calls'] += 1
        else:
            self.metrics['polygon_bounded_search_calls'] += 1
        self.metrics['solver_calls'] += sum(level['solver_calls'] for level in receipt.get('levels', []))
        self.events.append({'event': 'representative_search', 'receipt': receipt})
        deep_attempted = (deepen or (receipt.get('bounded_k') or 0) >= 4 or
                          any(level.get('k', 0) >= 4 for level in receipt.get('levels', [])))
        deep_failure = (not candidate_only and self.bounds is None and deep_attempted and
                        str(receipt.get('reason', '')).startswith(
                            ('solver_unknown', 'hard_timeout', 'solver_canceled')))
        if deep_failure:
            self._rep_deep_failures[rep] = self._rep_deep_failures.get(rep, 0) + 1
        elif not candidate_only:
            self._rep_deep_failures[rep] = 0
            self._depth_failures = 0
        if deep_failure:
            self._depth_failures += 1
            if receipt.get('bounded_k'):
                learned = min(4, receipt['bounded_k'])
                previous = self._rep_depth_caps.get(rep)
                if previous is None or learned < previous:
                    self._rep_depth_caps[rep] = learned
                    self.metrics['depth_caps_learned'] += 1
                    self.events.append({'event': 'depth_cap_learned', 'query': q,
                                        'representative': rep, 'cap': learned,
                                        'reason': receipt['reason']})
                if self._depth_failures >= 2 and (self._global_depth_cap is None or learned < self._global_depth_cap):
                    self._global_depth_cap = learned
                    self.metrics['global_depth_caps_learned'] += 1
                    self.events.append({'event': 'global_depth_cap_learned', 'query': q,
                                        'representative': rep, 'cap': learned,
                                        'failures': self._depth_failures})
        if time.perf_counter() >= call_deadline:
            self.metrics['late_evidence_rejected'] += 1
            self.pending_reason[q] = ('undecided', 'pair_or_batch_deadline')
            return
        if receipt['status'] == 'candidate':
            if self._install(receipt['database'], target=(q, rep),
                             admission_deadline=call_deadline, origin='polygon'):
                self.pending_reason[q] = ('undecided', 'new_fingerprint_requires_representative_check')
            else:
                self.pending_reason[q] = ('undecided', 'candidate_failed_engine_admission')
            return
        if candidate_only:
            self.pending_reason[q] = ('undecided', 'tail_proof_pending')
            return
        if receipt.get('bounded_k') and not cross_class:
            from .stream_numeric import bounded_numeric_admission
            receipt['numeric_admission'] = (
                bounded_numeric_admission(self.search.irs.get(q), self.search.irs.get(rep),
                                          self.dialect, self.schema, receipt['bounded_k'])
                if numeric_preflight.get('reason') == 'numeric_row_bound_missing'
                else numeric_preflight)
            if receipt['numeric_admission']['admitted']:
                admitted = self._add_edge(
                    Edge(q, rep, 'bounded', receipt, receipt['bounded_k'], receipt['domain_sha256']),
                    call_deadline)
                if admitted and caps and not deepen:
                    self._deferred_deepening.append(q)
                    self.events.append({'event': 'depth_deferred', 'query': q,
                                        'representative': rep, 'bounded_k': receipt['bounded_k']})
                return
        both_unsupported = receipt['status'] == 'unsupported' and native is not None and native.get('status') == 'unsupported'
        self.pending_reason[q] = ('unsupported' if both_unsupported else 'undecided',
                                  receipt.get('numeric_admission', {}).get('reason',
                                      receipt.get('reason', receipt['status'])))

    def _compare_in_fresh_pool(self, q, rep, **kwargs):
        """One pair decision on a pool built only from the seed databases.

        No library installed for an earlier pair is visible here. The local
        pool's evidence is merged back into the shared pool afterwards so the
        final partition keeps its certificates, but the next pair decision
        resets to the seed pool again: cross-pair reuse in decisions is zero.
        """
        saved = list(self.witnesses)
        seed = [w for w in saved if self._witness_origin.get(id(w)) == 'seed']
        seed_ids = {id(w) for w in seed}
        self.metrics['pool_resets'] += 1
        self.metrics['decision_pool_size'] = len(seed)
        local = []
        try:
            self.witnesses = list(seed)
            self._reindex()
            self._in_fresh_pool = True
            self._compare(q, rep, **kwargs)
        finally:
            self._in_fresh_pool = False
            local = [w for w in self.witnesses if id(w) not in seed_ids]
            for w in local:
                if all(w is not old for old in saved):
                    saved.append(w)
            self.witnesses = saved
            self._reindex()
            self._recompute_attribution()

    def _recompute_attribution(self):
        """Recompute per-library first-separations after a pool reset."""
        cats, overlap, pair_map = self.raw_split_events()
        for key in self.SEPARATION_CATEGORIES:
            self.metrics[key + '_separated_pairs'] = cats[key]
        self.metrics['polygon_library_separated_pairs'] = cats['polygon']
        self._lib_overlap_count = overlap
        self._lib_overlap_pair = {frozenset(k): v for k, v in pair_map.items()}
        self._last_split = None

    def count_transferred_labels(self):
        """Labels that reach a representative through more than one edge."""
        count = 0
        for q in self.arrival:
            rep = self.representatives.get(self.fingerprints.get(q))
            if rep is None or q == rep:
                continue
            path = self._path(q, rep)
            if path is not None and len(path) > 1:
                count += 1
        self.metrics['transferred_labels'] = count
        return count

    def close_direct_labels(self):
        """cert_off: replace every transitive label by a direct pair decision.

        Returns the number of remaining transfers; the runner asserts it is
        zero when certificate transfer is ablated.
        """
        if self.no_certificate_transfer:
            for q in list(self.arrival):
                rep = self.representatives.get(self.fingerprints.get(q))
                if rep is None or q == rep:
                    continue
                # A transitive relation is the only reason to spend a direct
                # decision here; an undecided pair stays undecided.
                path = self._path(q, rep, direct_only=False)
                if path is None or len(path) <= 1:
                    continue
                self._compare(q, rep=rep, force=True, refutation_only=False,
                              use_quick=False)
        return self.count_transferred_labels()

    def _component(self, a):
        seen = {a}; todo = deque([a])
        while todo:
            q = todo.popleft()
            for edge in self.adjacency.get(q, ()):
                if edge.status == 'bounded' and edge.domain != self.search.domain_identity():
                    continue
                nxt = edge.b if edge.a == q else edge.a
                if nxt not in seen and self.fingerprints[a] == self.fingerprints[nxt]:
                    seen.add(nxt);todo.append(nxt)
        return seen

    def _add_edge(self, edge, admission_deadline=None):
        cutoff = min(self.deadline, admission_deadline or self.deadline)
        if time.perf_counter() >= cutoff:
            self.metrics['late_evidence_rejected'] += 1
            self.pending_reason[edge.a] = ('undecided', 'pair_or_batch_deadline')
            return False
        assert self.fingerprints[edge.a] == self.fingerprints[edge.b]
        if edge.status == 'proved':
            gate = edge.receipt.get('admission', {})
            from .stream_prover import hashlib_sha
            assert gate.get('q1_sha256') == hashlib_sha(self.queries[edge.a])
            assert gate.get('q2_sha256') == hashlib_sha(self.queries[edge.b])
        previous_events = len(self.events)
        previous_adjacency = {q: (q in self.adjacency, list(self.adjacency.get(q, ())))
                              for q in (edge.a, edge.b)}
        previous_rng = self._rep_rng.getstate()
        self.edges.append(edge)
        self.events.append({'event': 'evidence_edge_admitted',
                            'wall_s': time.perf_counter()-self.started,
                            'a': edge.a, 'b': edge.b, 'status': edge.status})
        for q in (edge.a, edge.b):
            self.adjacency.setdefault(q, []).append(edge)
        fp = self.fingerprints[edge.a]
        rep = self.representatives[fp]
        candidate = (self._rep_rng.choice(sorted({edge.a, edge.b, rep})) if self.random_representative else
                     min((edge.a, edge.b, rep), key=lambda q: (self.complexity[q], self.arrival.index(q))))
        if candidate != rep:
            self.representatives[fp] = candidate
            self.events.append({'event': 'representative_replaced', 'old': rep, 'new': candidate})
        if time.perf_counter() >= cutoff:
            self.edges.pop()
            for q, (existed, edges) in previous_adjacency.items():
                if existed:
                    self.adjacency[q] = edges
                else:
                    self.adjacency.pop(q, None)
            self.representatives[fp] = rep
            self._rep_rng.setstate(previous_rng)
            del self.events[previous_events:]
            self.metrics['late_evidence_rejected'] += 1
            self.pending_reason[edge.a] = ('undecided', 'pair_or_batch_deadline')
            return False
        return True

    def deepen_deferred(self):
        # Admit the whole stream with individually certified shallow checks,
        # then spend remaining batch time on the postponed depth ladder.
        for q in dict.fromkeys(self._deferred_deepening):
            if self.remaining() <= self.finalization_reserve_s:
                break
            rep = self.representatives[self.fingerprints[q]]
            if q == rep or self.relation(q, rep)['status'] != 'bounded':
                continue
            old_k = self.relation(q, rep)['k']
            self.metrics['deferred_deepening_calls'] += 1
            self._compare(q, rep=rep, force=True, deepen=True, use_quick=False)
            if self.fingerprints[q] != self.fingerprints[rep]:
                self.metrics['deferred_deepening_upgrades'] += 1
            elif self.relation(q, rep)['status'] == 'bounded' and self.relation(q, rep)['k'] > old_k:
                self.metrics['deferred_deepening_upgrades'] += 1

    def refute_pending(self, search_budget=0.5):
        # The stream is fully admitted. Seek inexpensive counterexamples for
        # all unresolved classes before spending time on bounded proofs.
        for q in self.arrival:
            if self.remaining() <= self.finalization_reserve_s + search_budget:
                break
            if self.relation(q)['status'] == 'undecided':
                self.metrics['tail_candidate_calls'] += 1
                self._compare(q, candidate_only=True, use_quick=False,
                              search_budget=search_budget)

    def retry_pending(self):
        # A newly admitted witness can split an earlier pending query after its
        # turn. Revisit in arrival order while evidence changes; stop when a
        # complete pass adds no witness or equivalence edge. Under no_pool_reuse
        # every retry starts from the seed pool and can admit a fresh library
        # for the same unresolved pair; one pass bounds the ablation without
        # letting a non-converging retry sequence consume the batch budget.
        max_passes = 1 if self.no_pool_reuse else None
        passes = 0
        while self.remaining() > self.finalization_reserve_s:
            before = (len(self.witnesses), len(self.edges))
            attempted = 0
            for q in self.arrival:
                if self.remaining() <= self.finalization_reserve_s:
                    break
                if self.relation(q)['status'] == 'undecided':
                    attempted += 1
                    self.metrics['retries'] += 1
                    self._compare(q, use_quick=False)
            passes += 1
            self.metrics['retry_passes'] = self.metrics.get('retry_passes', 0) + 1
            if (not attempted or before == (len(self.witnesses), len(self.edges))
                    or (max_passes is not None and passes >= max_passes)):
                break

    def probe_without_pool(self, q, prior):
        """Ablation: pay for an independent pair check already pruned by W.

        The existing replayed W still defines the output partition. This
        probe cannot add evidence or alter it; it measures pairwise generation
        and replay cost when old certificates cannot prune a new comparison.
        """
        if self.fingerprints[q] == self.fingerprints[prior] or self.remaining() <= 0:
            return
        self.metrics['pair_decision_requests'] += 1
        call_deadline = min(self.deadline, time.perf_counter()+self.per_call_s)
        if self.prover is not None:
            self.metrics['prover_calls'] += 1
            phase_start = time.perf_counter()
            try:
                native = self.prover(self.queries[q], self.queries[prior],
                                     max(0., call_deadline-time.perf_counter()))
            finally:
                self.metrics['bounded_proof_s'] += time.perf_counter()-phase_start
            self.events.append({'event':'no_pool_native_probe','query':q,
                                'representative':prior,'receipt':native})
            if (native.get('status') == 'native_eq' and self.prover_admit is not None
                    and self.prover_admit(q, prior, native)):
                raise AssertionError('proved equivalence contradicts replayed pool witness')
        phase_start = time.perf_counter()
        receipt = self.search.search(q, prior, max(0., call_deadline-time.perf_counter()),
                                     bounds=self.bounds, canonical_contract=True)
        self.metrics['refutation_search_s' if receipt['status']=='candidate' else 'bounded_proof_s'] += time.perf_counter()-phase_start
        self.metrics['solver_calls'] += sum(level['solver_calls'] for level in receipt.get('levels', []))
        event={'event':'no_pool_independent_probe','query':q,'representative':prior,
               'receipt':receipt,'wall_s':time.perf_counter()-self.started}
        if receipt['status'] == 'candidate' and not self.schema.violations(receipt['database']):
            from seer2.backends import EngineRejected
            try:
                first=self._run(receipt['database'],[q,prior],phase='replay_s')
                second=self._run(receipt['database'],[q,prior],phase='replay_s')
                event['replay_separated']=(first[q].stable and first[prior].stable and
                    first[q].key != first[prior].key and
                    all(first[x].key == second[x].key for x in (q,prior)))
            except EngineRejected:
                event['replay_separated']=False
        self.events.append(event)

    def library_arrival_overlap(self):
        """Overlap pairs and their origins via the authoritative replayer."""
        _, _, pair_map = self.raw_split_events()
        return {frozenset(k): v for k, v in pair_map.items()}

    def assert_invariants(self, replay=True):
        try:
            return self._assert_invariants(replay)
        except AssertionError as exc:
            path = self._save_replay_context('invariant_failure', error=str(exc))
            raise AssertionError(f'{exc}; full replay context: {path}') from exc

    def _assert_invariants(self, replay=True):
        assert self.metrics['invalid_admitted_certificates'] == 0
        self.assert_quick_separation()
        # Raw overlap events must be a subset of the independently recomputed
        # overlap; counted per-origin events equal the tracked pair set.
        _, raw_overlap, raw_pairs = self.raw_split_events()
        raw_pairs = {frozenset(k): v for k, v in raw_pairs.items()}
        assert set(self._lib_overlap_pair) == set(raw_pairs), \
            'tracked overlap pairs differ from the replayed raw events'
        for key, origin in self._lib_overlap_pair.items():
            assert raw_pairs[key] == origin, 'overlap origin mismatch'
        assert self._lib_overlap_count == raw_overlap, \
            'counted overlap events disagree with the replayed raw events'
        # Censoring must not lose certified separations: every pair the final
        # partition separates has an active library column that separates it,
        # so its certificate is in the pool and the snapshot can count it.
        active = [w for w in self.witnesses if w.active]
        for i, a in enumerate(self.arrival):
            for b in self.arrival[i + 1:]:
                if self.fingerprints[a] == self.fingerprints[b]:
                    continue
                assert any(self._wkey(w, a) != self._wkey(w, b) for w in active), \
                    'separated pair without a library certificate'
        assert self.metrics['quick_witness_slow_calls'] == 0, \
            'quick step returned far slower than the released per-pair driver'
        assert len(self._arrival_requested) == len(
            [q for q in self._arrival_requested]), 'duplicate arrival request'
        assert self._arrival_requested <= set(self.arrival), \
            'arrival request for a query that never arrived'
        assert all(e['wall_s'] < self.deadline-self.started for e in self.events
                   if e.get('event') in ('arrival', 'pool_added',
                                         'evidence_edge_admitted'))
        for w in self.witnesses:
            assert not self.schema.violations(w.database)
            if replay and w.active:
                replayed = self._run(w.database, self.arrival, phase='replay_s')
                mismatches = {q: {'stored': w.observations[q], 'actual': replayed[q]}
                              for q in self.arrival
                              if not ((replayed[q].stable and replayed[q].key == w.observations[q].key)
                                      if w.mode == 'full' else replayed[q].cardinality == self._wkey(w, q))}
                if mismatches:
                    path = self._save_replay_context('final_replay_mismatch',
                        database=w.database, witness=w, replayed=replayed, mismatches=mismatches)
                    raise AssertionError(f'final replay mismatch: {list(mismatches)}; context: {path}')
        for fp, members in self.index.items():
            assert all(self.fingerprints[q] == fp for q in members)
        reps = list(self.representatives.values())
        for i, a in enumerate(reps):
            for b in reps[i+1:]:
                assert any(w.active and self._wkey(w, a) != self._wkey(w, b) for w in self.witnesses)
        for edge in self.edges:
            if edge.status == 'proved':
                assert edge.receipt['status'] == 'native_eq' and edge.receipt.get('evidence')
        return True

    def _refresh_quick_admissions(self):
        for pair, column in self._quick_admission_columns.items():
            if not self._quick_admitted.get(pair):
                continue
            a, b = pair
            if (column.active and self._wkey(column, a) != self._wkey(column, b)):
                continue
            # Removal of a column without a legitimate mode transition remains
            # an invariant violation, rather than being silently laundered.
            assert any(w is column for w in self.witnesses)
            self._quick_admitted[pair] = False
            self.metrics['quick_admissions_revoked'] = self.metrics.get('quick_admissions_revoked', 0) + 1
            self.events.append({'event': 'quick_admission_revoked', 'a': a, 'b': b,
                                'reason': 'column_mode_changed', 'mode': column.mode,
                                'active': column.active,
                                'wall_s': time.perf_counter()-self.started})

    def assert_quick_separation(self):
        """Runner assertion: a quick-step separation is never lost.

        For each pair brought to the official quick step while the pair shared
        a class, either the final partition separates it, or the pair is still
        undecided within its class, or completion found an equality edge for
        it; in the last two cases the quick step admitted no library that could
        separate the pair, and active libraries none either.
        """
        checked = held = 0
        for a, b in self._quick_query_pairs:
            if a not in self.fingerprints or b not in self.fingerprints:
                continue
            if self.fingerprints[a] != self.fingerprints[b]:
                checked += 1
                continue
            pending = (self.pending_reason.get(a, ('',))[0] == 'undecided'
                       or self.pending_reason.get(b, ('',))[0] == 'undecided')
            assert self._path(a, b) is not None or pending, \
                'quick pair rejoined without an equivalence edge'
            assert not self._quick_admitted.get((a, b)), 'admitted quick library rejoined its pair'
            assert not any(w.active and self._wkey(w, a) != self._wkey(w, b)
                           for w in self.witnesses), 'quick pair rejoined under a separating library'
            held += 1
        self.metrics['quick_separation_checked'] = checked
        self.metrics['quick_separation_joined_at_completion'] = held
        return True

    def snapshot(self):
        if hasattr(self.search, 'close_worker'):
            self.search.close_worker()
        from collections import Counter
        from math import comb
        def connected_pairs(only_proved):
            parent = {q:q for q in self.arrival}
            def root(q):
                while parent[q] != q:
                    parent[q] = parent[parent[q]]
                    q = parent[q]
                return q
            for edge in self.edges:
                if edge.status != 'proved' and (only_proved or edge.domain != self.search.domain_identity()):
                    continue
                if self.fingerprints[edge.a] == self.fingerprints[edge.b]:
                    parent[root(edge.a)] = root(edge.b)
            return sum(comb(n,2) for n in Counter(root(q) for q in self.arrival).values())
        n = len(self.arrival)
        class_sizes = sorted((len(v) for v in self.index.values()), reverse=True)
        total_pairs = comb(n,2)
        within_pairs = sum(comb(n,2) for n in class_sizes)
        proved_pairs = connected_pairs(True)
        admitted_pairs = connected_pairs(False)
        pair_counts = {'separated':total_pairs-within_pairs,'proved':proved_pairs,
                       'bounded':admitted_pairs-proved_pairs,
                       'undecided':within_pairs-admitted_pairs}
        assert all(x >= 0 for x in pair_counts.values()) and sum(pair_counts.values()) == total_pairs
        receipt_index = {id(edge.receipt): i for i, edge in enumerate(self.edges)}
        relations = {}
        for q in self.arrival:
            relation = self.relation(q)
            relation['evidence_edges'] = [receipt_index[id(r)] for r in relation.pop('receipts', [])]
            relations[q] = relation
        member_counts = dict(Counter(v['status'] for v in relations.values()))
        return {'contract': CONTRACT, 'classes': list(self.index.values()),
                'representatives': list(self.representatives.values()),
                'relations': relations,
                'class_sizes':class_sizes,'class_size_histogram':dict(Counter(class_sizes)),
                'class_count':len(class_sizes),'pair_counts':pair_counts,
                'member_status_counts':member_counts,
                'domain_identity': self.search.domain_identity(),
                'evidence_edges': [{'a': e.a, 'b': e.b, 'status': e.status, 'k': e.k,
                                    'domain': e.domain, 'receipt': e.receipt} for e in self.edges],
                'metrics': {**self.metrics, 'pool_size': len(self.witnesses),
                    'first_fingerprint_separated_pair_fraction':
                        self.metrics['first_fingerprint_separated_pairs']/total_pairs if total_pairs else 0.0,
                    'first_fingerprint_new_class_query_fraction':
                        (max(0, self.metrics['fingerprint_misses']-1)/(n-1)
                         if n>1 else 0.0),
                    'active_pool_size': sum(w.active for w in self.witnesses),
                    'wall_s': time.perf_counter()-self.started,
                    'cpu_s': self._cpu()-self.cpu_started,
                    'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss},
                'events': self.events}
