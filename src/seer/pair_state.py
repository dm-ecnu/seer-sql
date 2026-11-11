"""Pair-indexed candidate for nontransitive comparison contracts.

Residual connected components are execution scheduling units, not equivalence classes.
The original Seer implementation and frozen runners are unchanged.
"""
from __future__ import annotations

import itertools
import time
import z3
from seer2.backends import EngineRejected
from .core import Seer, loadable_order, _jsonable, _block, from_jsonable
from .core import split_class_n
from .encode import Domain, schema_strings
from .ir import Unsupported
from .symbolic import Level


class PairSeer(Seer):
    def __init__(self, *args, outcome_memo=False, shared_pair_replay=False, share_query_encoding=True, encode_sigma=True, witness_reuse=True, goal_policy="all_pairs", **kwargs):
        super().__init__(*args, **kwargs)
        self.share_query_encoding = bool(share_query_encoding)
        self.encode_sigma = bool(encode_sigma)
        self.rejected_symbolic_instances = []
        if goal_policy not in ('all_pairs','safe_forest'):raise ValueError('unknown goal policy')
        if goal_policy=='safe_forest' and self.symbolic_selection!='representatives':
            raise ValueError('safe forest preserves feasibility, not the max_pairs objective')
        self.goal_policy=goal_policy
        self.goal_selection_trace=[]
        self.witness_reuse = bool(witness_reuse)
        self._witness_cursor = 0
        self._proposal_target = None
        self.outcome_memo = bool(outcome_memo)
        self.shared_pair_replay = bool(shared_pair_replay) and self.witness_reuse
        self._pair_event_index = {}
        self._indexed_events = 0
        self.open_pairs = {frozenset(p) for p in itertools.combinations(self.queries, 2)
                           if not any(q in self.invalid for q in p)}
        self.late_instances = []
        self._components()

    def pending_pairs(self):
        return [tuple(sorted(p)) for p in sorted(self.open_pairs, key=lambda p: tuple(sorted(p)))]

    def _components(self):
        adjacency = {q: set() for q in self.queries}
        for pair in self.open_pairs:
            a, b = tuple(pair)
            adjacency[a].add(b)
            adjacency[b].add(a)
        unseen = set(self.queries)
        components = []
        for q in self.queries:
            if q not in unseen:
                continue
            unseen.remove(q)
            component, stack = [], [q]
            while stack:
                v = stack.pop()
                component.append(v)
                for w in sorted(adjacency[v]):
                    if w in unseen:
                        unseen.remove(w)
                        stack.append(w)
            components.append(sorted(component))
        self.partition = components

    def unsplit_pairs(self):
        return len(self.open_pairs)

    def _settled(self, component):
        members = set(component)
        return all(pair in self.proved or pair in self.full
                   for pair in self.open_pairs if pair <= members)

    def witness_pairs(self, pairs):
        """Choose a proposal's certification scope before native validation.

        Symbolic predictions supply a focal pair. Heuristic proposals without
        a focal pair rotate deterministically, including unsuccessful targets.
        No native execution is used to choose the most favorable target.
        """
        if self.witness_reuse:
            return pairs
        if self._proposal_target is not None:
            target=tuple(self._proposal_target)
            assert target in pairs
        else:
            target=pairs[self._witness_cursor % len(pairs)]
            self._witness_cursor+=1
        return [target]

    def run_db(self, db, origin):
        pairs = self.pending_pairs()
        if not pairs:
            return 0
        if self.elapsed() >= self.budget:
            self.stats.add("budget_skipped_db")
            return 0
        pairs = self.witness_pairs(pairs)
        self.stats.add("witness_proposals")
        self.stats.add("witness_target_pairs",len(pairs))
        live_set = set().union(*(set(p) for p in pairs))
        live = [q for q in self.queries if q in live_set]
        db, acyclic = loadable_order(self.S, db, report=True)
        if not acyclic and not getattr(self.backend, "deferred_fks", True):
            self.stats.add("cyclic_reference_skipped")
            return 0
        t0 = time.perf_counter()
        try:
            inst = self.backend.open(db)
        except EngineRejected:
            self.stats.add(f"engine_rejected_{origin}")
            return 0
        try:
            outs = self._exec_all(inst, db, live)
        finally:
            inst.close()
        self.timing.add("exec_s", time.perf_counter() - t0)
        if self.elapsed() >= self.budget:
            self.late_instances.append({"origin": origin, "db": _jsonable(db),
                                        "elapsed_s": round(self.elapsed(), 3)})
            self.stats.add("over_budget_db")
            return 0
        if self.outcome_memo:
            from .outcome_memo import separated_pairs
            separated, comparison_stats = separated_pairs(pairs, outs)
            for key, value in comparison_stats.items():
                if key == "comparison_cache_size":
                    self.stats["comparison_cache_peak"] = max(self.stats.get("comparison_cache_peak", 0), value)
                else:
                    self.stats.add(key, value)
        else:
            separated = [p for p in pairs if len(split_class_n(list(p), outs)) == 2]
        if separated:
            idx = len(self.dbs)
            self.dbs.append(_jsonable(db))
            self.stats.add(f"cert_dbs_{origin}")
            for a, b in separated:
                self.events.append((idx, [a, b], [[a], [b]]))
                self.outs[idx, a], self.outs[idx, b] = outs[a], outs[b]
                self.open_pairs.remove(frozenset((a, b)))
            self.last_split_s = self.elapsed()
            self._components()
        return len(separated)

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
                           per_query_s=max(2.0, self.budget / 12),
                           share_query_encoding=self.share_query_encoding, encode_sigma=self.encode_sigma)
            except MemoryError:
                self.stats.add("level_oom")
                break
            except (Unsupported, z3.Z3Exception, RecursionError) as ex:
                # Σ itself is outside the fragment: no level can be built; every open class stays undecided
                why = f"{ex.code}: {ex}" if isinstance(ex, Unsupported) else type(ex).__name__
                for c in self.pending_pairs():
                    self.reasons[frozenset(c)] = f"k={k}: schema constraints not encodable ({why})"
                self.stats.add("sigma_unsupported")
                break
            self.timing.add("encode_s", lv.encode_s)
            self.stats.add("levels")
            for name, value in lv.enc.stats.items():
                self.stats.add('encoding_'+name, value)
            mismatch = 0
            while self.multi() and self.elapsed() < self.budget:
                classes = [list(p) for p in self.pending_pairs() if all(lv.ok(q) for q in p)]
                if not classes:
                    break
                try:
                    classes,goal_info=lv.goal_pairs(classes,self.goal_policy)
                    self.goal_selection_trace.append({'k':k,**goal_info})
                    self.stats.add('symbolic_goal_pairs_input',goal_info['input_pairs'])
                    self.stats.add('symbolic_goal_pairs_selected',goal_info['selected_pairs'])
                    self.stats.add('symbolic_goal_pairs_omitted',goal_info['omitted_pairs'])
                    status, m, pred = lv.find(classes, budget_s=max(1.0, (self.budget - self.elapsed()) / 4),
                                              selection=self.symbolic_selection)
                    if self.symbolic_selection == "max_pairs":
                        self.symbolic_selection_trace.append({"k": k, **lv.selection_info})
                except (TimeoutError, MemoryError):
                    status, m, pred = "unknown", None, None
                self.stats.add("solver_calls", lv.checks)
                lv.checks = 0
                if self.elapsed() >= self.budget:
                    self.stats.add("over_budget_solver_result")
                    break
                if status == "unsat":
                    if mismatch:
                        # Database blockers remove legal inputs. UNSAT of that
                        # restricted search cannot prove the original domain.
                        for c in self.pending_pairs():
                            self.reasons[frozenset(c)] = f"k={k}: excluded_unconfirmed_models"
                        self.stats.add("bounded_proof_withheld")
                        self.log(f"k={k}: restricted UNSAT after model mismatch; proof withheld")
                        break
                    for pair in self.pending_pairs():
                        key = frozenset(pair)
                        if all(lv.ok(q) for q in pair):
                            self.proved[key] = k
                        else:
                            self.reasons[key] = f"k={k}: pair endpoint not encodable"
                    self.log(f"k={k}: UNSAT -> only encoded residual pairs bounded at k")
                    break
                if status == "unknown":
                    for c in self.pending_pairs():
                        self.reasons[frozenset(c)] = f"k={k}: solver unknown/timeout"
                    self.stats.add("solver_unknown")
                    break
                db = lv.decode(m, self.S)
                viol = self.S.violations(db)
                self.stats.add("symbolic_candidates")
                if viol:
                    assert not self.encode_sigma, f"symbolic model violates Σ: {viol[:3]}"
                    self.record_sigma_rejection(db, k, viol)
                    g = 0
                else:
                    self.stats.add("symbolic_dbs")
                    if not self.witness_reuse:
                        self._proposal_target=next((tuple(c) for c,blocks in zip(classes,pred or ()) if len(blocks)>1),None)
                    try:
                        g = self.run_db(db, f"sym_k{k}")
                    finally:
                        self._proposal_target=None
                if g == 0:
                    mismatch += 1
                    self.stats.add("model_mismatch")
                    self.log(f"k={k}: model predicted a split the engine did not confirm ({mismatch})")
                    if mismatch >= 3:
                        for c in self.pending_pairs():
                            self.reasons[frozenset(c)] = f"k={k}: encoding/engine mismatch"
                        break
                    # exclude this exact database and retry
                    lv.solver.add(_block(lv, m))
            self.timing.add("solve_s", lv.solve_s)
            self.timing.add("level_s", time.perf_counter() - t0)


    def record_sigma_rejection(self, db, k, violations):
        """Retain every rejected model; original full-Schema acceptance is unchanged."""
        self.stats.add("symbolic_sigma_rejected")
        self.rejected_symbolic_instances.append({'k': k, 'elapsed_s': self.elapsed(),
            'db': _jsonable(db), 'violations': list(violations)})

    def _shareable_replay_query(self, q):
        # SQL query semantics are read-only. Unknown/UDF or session-changing
        # forms retain legacy per-pair replay rather than sharing a snapshot.
        from sqlglot import exp
        tree = self.prep[q].tree
        while isinstance(tree, exp.Subquery):
            tree = tree.this
        if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            return False
        forbidden = {'Insert', 'Update', 'Delete', 'Merge', 'Create', 'Drop', 'Alter', 'Command', 'Into', 'Lock', 'PropertyEQ', 'SessionParameter'}
        volatile = {'RAND', 'RANDOM', 'UUID', 'NEXTVAL', 'NEXT_VALUE_FOR', 'SETVAL', 'SLEEP', 'PG_SLEEP',
                    'CURRENT_TIMESTAMP', 'CURRENT_DATE', 'CURRENT_TIME', 'CURRENT_DATETIME', 'SYSDATE',
                    'CURRENT_USER', 'SESSION_USER', 'CURRENT_SCHEMA', 'CURRENT_DATABASE', 'LAST_INSERT_ID'}
        pure = {'ABS', 'ROUND', 'CEIL', 'CEILING', 'FLOOR', 'LOWER', 'UPPER', 'LENGTH', 'CHAR_LENGTH',
                'COALESCE', 'IF', 'IFNULL', 'NULLIF', 'CASE', 'CAST', 'TRY_CAST', 'CONVERT', 'EXTRACT',
                'DATE', 'DATE_ADD', 'DATE_SUB', 'DATE_DIFF', 'DATEDIFF', 'TIMESTAMP_DIFF', 'TIME_TO_STR',
                'STR_TO_DATE', 'STR_TO_TIME', 'YEAR', 'MONTH', 'DAY', 'WEEK', 'QUARTER', 'CONCAT',
                'CONCAT_WS', 'SUBSTRING', 'REPLACE', 'TRIM', 'LEFT', 'RIGHT', 'MOD', 'POWER', 'SQRT',
                'EXP', 'LN', 'LOG', 'LEAST', 'GREATEST', 'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'LAG',
                'LEAD', 'FIRST_VALUE', 'LAST_VALUE', 'NTH_VALUE', 'NTILE', 'PERCENT_RANK', 'CUME_DIST', 'EXISTS', 'IN', 'TS_OR_DS_TO_DATE', 'TS_OR_DS_TO_DATETIME',
                'TS_OR_DS_TO_TIMESTAMP', 'DATE_STR_TO_DATE', 'TIME_STR_TO_TIME', 'TIME_STR_TO_DATE'}
        for node in tree.walk():
            if type(node).__name__ in forbidden or isinstance(node, exp.Anonymous):
                return False
            if isinstance(node, (exp.Binary, exp.Unary, exp.Predicate)):
                continue
            if isinstance(node, exp.Func):
                name = node.sql_name().upper()
                if name in volatile:
                    return False
                if q not in self.ir and not isinstance(node, exp.AggFunc) and name not in pure:
                    return False
        return True

    def _shared_recheck(self):
        """Fresh replay per witness/attempt, with independent pair withdrawal.

        Events remain pair-indexed. Query execution is shared only inside one
        read-only snapshot; output comparison and the retry bound stay per edge.
        A successful edge leaves the retry frontier even if other edges fail.
        """
        t0 = time.perf_counter()
        groups = {}
        safe = {q: self._shareable_replay_query(q) for q in self.queries}
        for pos, (idx, cls, blocks) in enumerate(self.events):
            if pos in self.dropped:
                continue
            assert len(cls) == len(set(cls)) == 2, 'pair replay requires two endpoints'
            key = (idx, None) if all(safe[q] for q in cls) else (idx, pos)
            groups.setdefault(key, []).append(pos)
        for (idx, _), pending in groups.items():
            for attempt in range(self.recheck_tries):
                live = {q for pos in pending for q in self.events[pos][1]}
                qs = [q for q in self.queries if q in live]
                assert set(qs) == live
                db = from_jsonable(self.dbs[idx])
                inst = self.backend.open(db)
                self.stats.add('recheck_group_loads')
                try:
                    outs = self._exec_all(inst, db, qs)
                finally:
                    inst.close()
                failed = []
                for pos in pending:
                    _, cls, blocks = self.events[pos]
                    got = sorted(sorted(b) for b in split_class_n(cls, outs))
                    if got == sorted(blocks):
                        self.stats.add('recheck_ok')
                    else:
                        self.stats.add('recheck_err' if any(outs[q].kind != 'ok' for q in cls)
                                       else 'recheck_differs')
                        failed.append(pos)
                pending = failed
                if not pending:
                    break
            for pos in pending:
                self.dropped.add(pos)
                self.stats.add('recheck_dropped')
                self.log(f'pair certificate event {pos} not reproduced in {self.recheck_tries} replays: withdrawn')
        self.timing.add('recheck_s', time.perf_counter() - t0)

    def pair_certificate(self, a, b):
        if not self.shared_pair_replay:
            return super().pair_certificate(a, b)
        # The event log is append-only; withdrawal changes only dropped.
        # Index each appended event once, then look up only this pair's proofs.
        if self._indexed_events > len(self.events):
            self._pair_event_index.clear()
            self._indexed_events = 0
        for pos in range(self._indexed_events, len(self.events)):
            idx, cls, blocks = self.events[pos]
            assert len(cls) == len(set(cls)) == 2
            positions = {q: i for i, block in enumerate(blocks) for q in block}
            assert sorted(q for block in blocks for q in block) == sorted(cls)
            if positions[cls[0]] != positions[cls[1]]:
                self._pair_event_index.setdefault(frozenset(cls), []).append((pos, idx))
        self._indexed_events = len(self.events)
        for pos, idx in self._pair_event_index.get(frozenset((a, b)), ()):
            if pos not in self.dropped:
                return idx
        return None

    def recheck(self):
        if self.shared_pair_replay:
            self._shared_recheck()
        else:
            super().recheck()
        valid = {frozenset(cls) for pos, (_, cls, _) in enumerate(self.events) if pos not in self.dropped}
        self.open_pairs = {frozenset(p) for p in itertools.combinations(self.queries, 2)
                           if not any(q in self.invalid for q in p) and frozenset(p) not in valid}
        self._components()
        self._terminal()

    def _terminal(self):
        if not self.open_pairs:
            self.stop = "all_split"
        elif self.stats.get("solver_out_of_memory"):
            self.stop = "resource_limit"
        elif all(p in self.proved or p in self.full for p in self.open_pairs):
            self.stop = "bounded_equivalent"
        elif self.elapsed() >= self.budget:
            self.stop = "budget"
        else:
            self.stop = "undecided"

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
            resource_stop = True
            self.stats.add("solver_out_of_memory")
            for p in self.open_pairs:
                if p not in self.proved and p not in self.full:
                    self.reasons[p] = "solver_out_of_memory"
        if not resource_stop and self.use_fast and any(not self._settled(c) for c in self.multi()):
            t0 = time.perf_counter()
            self._close(self.t0 + self.budget)
            self.timing.add("refine_close_s", time.perf_counter() - t0)
        if not resource_stop and self.prover is not None:
            for p in self.pending_pairs():
                t0 = time.perf_counter()
                if self.prover(self.S, [self.queries[q] for q in p], self.dialect) is True:
                    self.full.add(frozenset(p))
                self.timing.add("prover_s", time.perf_counter() - t0)
        self._terminal()
        self.recheck()
        return self.result()

    def verdict(self, a, b):
        if any(q in self.invalid for q in (a, b)):
            return {"verdict": "undecided", "reason": "query_error"}
        cert = self.pair_certificate(a, b)
        if cert is not None:
            return {"verdict": "split", "cert": cert}
        key = frozenset((a, b))
        if key in self.full:
            return {"verdict": "proved_equivalent"}
        if key in self.proved:
            return {"verdict": "bounded_equivalent", "k": self.proved[key]}
        for q in (a, b):
            if q in self.unsupported:
                return {"verdict": "undecided", "reason": "unsupported:" + self.unsupported[q][0]}
        return {"verdict": "undecided", "reason": self.reasons.get(key, self.stop or "budget")}

    def add_query(self, qid, sql):
        self._register(qid, sql)
        self._screen([qid])
        if qid not in self.invalid:
            self.open_pairs.update(frozenset((qid, q)) for q in self.queries
                                   if q != qid and q not in self.invalid)
        self._components()
        from .core import from_jsonable
        if self.witness_reuse:
            for db in list(self.dbs):
                self.run_db(from_jsonable(db), "incremental_replay")
        self.stop = None
        return self.run()

    def result(self):
        result = super().result()
        result["state_model"] = "residual_pair_graph"
        result["encoding_configuration"] = {'share_query_encoding': self.share_query_encoding,
                                             'encode_sigma': self.encode_sigma}
        result["rejected_symbolic_instances"] = self.rejected_symbolic_instances
        result["goal_selection_configuration"]={"policy":self.goal_policy,"objective":"bounded existential separation"}
        result["goal_selection_trace"]=self.goal_selection_trace
        result["witness_configuration"] = {"cross_pair_certificate_reuse":self.witness_reuse,
            "stored_database_incremental_reuse":self.witness_reuse,
            "heuristic_target_policy":None if self.witness_reuse else "round_robin_without_native_lookahead",
            "symbolic_target_policy":None if self.witness_reuse else "first_predicted_split_pair",
            "batch_candidate_generators_retained":True}
        result["residual_components"] = result.pop("classes")
        result["residual_pair_count"] = len(self.open_pairs)
        result["proved_pairs"] = [{"pair": sorted(p), "k": k} for p, k in self.proved.items()]
        result["late_instances"] = self.late_instances
        result.pop("proved")
        return result
