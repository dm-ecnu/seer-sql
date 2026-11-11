"""Shared canonical contract for stream SEER and the native SQLWitness pair shell."""
from __future__ import annotations
import itertools
import json
import os
from pathlib import Path
import random
import select
import signal
import subprocess
import tempfile
import time
from .stream import StreamPartition
from .stream_prover import NativeSQLSolver


def native_pair_tool(tool, eval_schema, dialect, sql_a, sql_b, budget, seed, call_id,
                     stages=None, session=None):
    """Run a released pair tool; leave acceptance to the shared partition."""
    from seereval import tools
    from seereval.engine import _coerce
    request = tools.build_request(tool, eval_schema,
        'postgresql' if dialect == 'postgres' else dialect,
        [sql_a, sql_b], budget, seed)
    if stages is not None:
        request['stages'] = stages
    if os.environ.get('SEER_ENTRY_METRICS'):
        request.update(_entry_metric_tool=tool, _entry_metric_call_id=call_id)
    native = session.call(request, budget) if session is not None else tools.call(tool, request, hard_s=budget)
    database = None
    if native['verdict'] == 'neq' and native.get('instance') is not None:
        original = native['instance']
        database = {}
        for table in eval_schema.tables:
            rows = next((v for k, v in original.items() if k.lower() == table.name.lower()), [])
            database[table.name.lower()] = []
            for row in rows:
                low = {str(k).lower(): v for k, v in row.items()}
                database[table.name.lower()].append({c['name'].lower(): _coerce(low.get(c['name'].lower()), c['type'])
                                                     for c in table.columns})
    return {'status': 'candidate' if database is not None else 'no_witness',
            'native': native, 'database': database}


class PersistentPairTool:
    """One released Python tool process for one arrival stream."""

    def __init__(self, tool, evidence_dir):
        from seereval import tools
        if tool not in ('sqlwitness', 'parseval'):
            raise ValueError('persistent pair tool unavailable: ' + tool)
        self.python = tools.TOOLS[tool]['py']
        self.worker = str(Path(tools.WORKERS) / f'{tool}_persistent_worker.py')
        self.scratch = tempfile.TemporaryDirectory(prefix=f'seer-{tool}-session-',
                                                   dir=os.environ['TMPDIR'])
        self.log = (Path(evidence_dir).parent / f'{tool}-persistent-stderr.log').open('ab')
        self.process = None
        self.starts = 0

    def _start(self):
        command = ['prlimit', '--as=8589934592', '--', self.python, '-u', self.worker]
        self.process = subprocess.Popen(command, cwd=self.scratch.name, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.log,
                                        start_new_session=True, bufsize=0)
        self.starts += 1

    def _stop(self):
        proc = self.process
        self.process = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        finally:
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    stream.close()

    def call(self, request, budget):
        started = time.perf_counter()
        if budget <= 0:
            return {'verdict': 'timeout', 'reason': 'batch_budget_before_call', 'wall': 0.0}
        if self.process is None or self.process.poll() is not None:
            self._stop()
            self._start()
        proc = self.process
        try:
            proc.stdin.write((json.dumps(request, default=str) + '\n').encode())
            proc.stdin.flush()
            remaining = budget - (time.perf_counter() - started)
            if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                self._stop()
                return {'verdict': 'timeout', 'reason': 'persistent_worker_deadline',
                        'wall': time.perf_counter() - started}
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError('persistent SQLWitness worker exited without response')
            response = json.loads(line)
            response['wall'] = time.perf_counter() - started
            return response
        except (BrokenPipeError, OSError, ValueError, RuntimeError) as exc:
            self._stop()
            return {'verdict': 'error', 'reason': type(exc).__name__ + ': ' + str(exc)[:200],
                    'wall': time.perf_counter() - started}

    def close(self):
        self._stop()
        self.log.close()
        self.scratch.cleanup()


def released_dialect(dialect):
    """Name the released SQLWitness side knows: it ships PostgreSQL support as
    dialect 'postgresql', while our engine spells the backend 'postgres'."""
    return 'postgresql' if dialect in ('postgres', 'postgresql') else dialect


def official_quick_pair(eval_schema, dialect, sql_a, sql_b, budget, seed, call_id, backend,
                        context=None):
    """Quick step through the released per-pair driver (same entry as the opponent).

    Writes the official one-pair input with the artifact's own materialize()
    loader and runs the same driver the opponent method uses; the counterexample
    is read back from the driver's CSV. No hand-built tables/constraints call
    into counterexample(), which is what made the old worker saturate 60 s on
    pairs the official driver answers in 0.02-2 s.
    """
    import tempfile
    from seereval.schema import to_ddl
    from seer2.schema import Schema
    import seer_sqlwitness
    from .official_batch import run_official_batch
    started = time.perf_counter()
    scratch = tempfile.mkdtemp(prefix='seer-official-quick-', dir=os.environ['TMPDIR'])
    # The released SQLWitness artifact supports PostgreSQL (its benchmarks ship
    # minidev-postgresql with dialect "postgresql"), but our engine spells the
    # backend "postgres"; SQLTYPE and to_ddl only know "postgresql". Normalise
    # for the released side and keep the seer2 schema dialect as given.
    released = released_dialect(dialect)
    try:
        schema = Schema(';\n'.join(to_ddl(eval_schema, released, deferrable=False)) + ';',
                        dialect, inclusions=seer_sqlwitness.sigma_extra(eval_schema))
        # The released driver answers a pair inside its batch context (shared
        # process, RNG stream, query order). Materialise the queries that have
        # arrived so far, as the opponent method does: a two-query input loses
        # the counterexample on 28% of the official-neq hard pairs.
        # Keep the pair under its own ids: deduplicating SQL strings (e.g. a
        # candidate identical to the reference) collapsed the pair and made the
        # witness selection look at another query's row.
        # Keep the requested pair under its own ids at the end of the query
        # list; deduplicating SQL strings collapsed the pair when a candidate
        # equalled the reference (0.72 -> 0.89 on the 1,272 hard pairs).
        ctx = list(context or [])
        queries = [(f'q{i}', sql) for i, sql in enumerate(ctx)]
        queries += [('qpair_a', sql_a), ('qpair_b', sql_b)]
        ia, ib = len(queries) - 2, len(queries) - 1
        engine, result = run_official_batch(
            schema, eval_schema, queries, dialect, backend,
            budget, seed, 'source', time.perf_counter(), scratch, method_seed=0)
        wall = time.perf_counter() - started
        # Pick the accepted witness whose own observations separate THIS pair;
        # the nested run may admit libraries for other CSV rows first.
        database = None
        for w in engine.witnesses:
            if not (w.active and w.database):
                continue
            obs = getattr(w, 'observations', {}) or {}
            ka, kb = 'qpair_a', 'qpair_b'
            if ka in obs and kb in obs and obs[ka].key != obs[kb].key:
                database = w.database
                break
        # Fall back to the released CSV's last row when no admitted witness
        # separates the pair by observation keys.
        if database is None:
            import csv as _csv
            csv_path = Path(scratch) / 'official-batch.csv'
            if csv_path.exists():
                rows = list(_csv.DictReader(csv_path.open()))
                if rows and rows[-1].get('Refuted') == 'True':
                    from .official_batch import parse_counterexample
                    from seereval.engine import _coerce
                    original = parse_counterexample(rows[-1].get('Counterexample') or '')
                    if original:
                        database = {}
                        for table in eval_schema.tables:
                            raw = next((v for k, v in original.items()
                                        if k.lower() == table.name.lower()), [])
                            database[table.name.lower()] = [
                                {c['name'].lower(): _coerce({str(k).lower(): v for k, v in row.items()}
                                                             .get(c['name'].lower()), c['type'])
                                 for c in table.columns} for row in raw]
        calls = [e for e in result['events'] if e.get('event') == 'official_batch_call']
        native_s = calls[0].get('elapsed_s') if calls else None
        return {'status': 'candidate' if database is not None else 'no_witness',
                'native': {'verdict': 'neq' if database is not None else 'no_refutation',
                           'tool_secs': native_s},
                'database': database, 'wall': wall,
                'nested_small_db_executions': engine.metrics['small_db_executions']}
    finally:
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)


def native_sqlwitness(eval_schema, dialect, sql_a, sql_b, budget, seed, call_id,
                      session=None, backend='sqlite', context=None):
    if session is None and eval_schema is not None:
        return official_quick_pair(eval_schema, dialect, sql_a, sql_b, budget,
                                   seed, call_id, backend, context=context)
    return native_pair_tool('sqlwitness', eval_schema, dialect, sql_a, sql_b,
                            budget, seed, call_id, session=session)


def run_stream(schema, queries, dialect, backend, budget_s, seed, native_runtime,
               evidence_dir, order='source', bounds=None, random_dbs=2,
               eval_schema=None, quick_sqlwitness=False, start_time=None,
               enable_prover=True, random_representative=False,
               no_fingerprint_grouping=False, no_pool_reuse=False,
               method_seed=0, schedule='arrival', candidate_budget_s=0.5,
               quick_failure_cap=None, separation_only=False, per_call_s=60.,
               quick_schedule='compare', remainder_quick_comparisons=False,
               no_certificate_transfer=False, single_bucket_target=False,
               search=None, class_off=False):
    if schedule not in ('arrival', 'tail_proof', 'microbatch', 'class_bounded'):
        raise ValueError('unknown stream proof schedule')
    if class_off and schedule != 'class_bounded':
        raise ValueError('class_off requires the class_bounded encoding base')
    if quick_schedule not in ('compare', 'arrival'):
        raise ValueError('unknown quick-step schedule')
    if remainder_quick_comparisons and schedule != 'tail_proof':
        raise ValueError('remainder comparisons only exist in the tail_proof schedule')
    sqls = list(queries)
    if order == 'reverse':
        sqls.reverse()
    elif order == 'shuffle':
        random.Random(seed).shuffle(sqls)
    elif order != 'source':
        raise ValueError('unknown arrival order')
    prover = (NativeSQLSolver.from_json(native_runtime, schema=schema, dialect=dialect,
        backend=backend, evidence_dir=evidence_dir) if enable_prover else None)
    if quick_sqlwitness and eval_schema is None:
        raise ValueError('official SQLWitness needs the source schema')
    quick_calls = [0]
    # Defect 10-29: the persistent SQLWitness worker never answered a pair
    # request within the batch budget (708 censored-batch calls returned
    # persistent_worker_deadline at ~62 s), while the released per-pair driver
    # answers the same pairs in 0.02-2 s. Use the released per-pair driver for
    # every quick call until the persistent transport is fixed.
    quick_session = None
    quick_mode = quick_sqlwitness and schedule not in ("microbatch", "class_bounded")
    def quick(sql_a, sql_b, budget):
        call_id = quick_calls[0]
        quick_calls[0] += 1
        return native_sqlwitness(eval_schema, dialect, sql_a, sql_b,
                                 budget, method_seed+1000*call_id, f'quick-{call_id}',
                                 session=quick_session, backend=backend,
                                 context=[sql for _, sql in engine.queries.items()][:12])
    engine = StreamPartition(schema, dialect, backend, budget_s=budget_s, seed=method_seed,
                             per_call_s=per_call_s, prover=prover, bounds=bounds, random_dbs=random_dbs,
                             quick_witness=quick if quick_sqlwitness and schedule not in ("microbatch", "class_bounded") else None,
                             start_time=start_time,random_representative=random_representative,
                             quick_failure_cap=quick_failure_cap if quick_sqlwitness else None,
                             remainder_quick_comparisons=remainder_quick_comparisons,
                             no_certificate_transfer=no_certificate_transfer,
                             single_bucket_target=single_bucket_target,
                             no_fingerprint_grouping=no_fingerprint_grouping,
                             no_pool_reuse=no_pool_reuse, search=search)
    engine.evidence_dir = str(Path(evidence_dir).parent) if evidence_dir is not None else None
    engine._class_arrival_only = schedule == 'class_bounded'
    engine._class_arrival_pairs = []
    potential = {}
    for q, _ in sqls:
        members = engine.index.get(engine.fingerprints.get(q))
        potential[q] = (members[-1] if members else q, engine.fingerprints.get(q))
    if quick_mode and quick_schedule == 'arrival':
        # Immediately after admission and before any equivalence proof, every
        # arrival that still shares its class with an earlier query takes one
        # official SQLWitness quick step against the class representative.
        # The proposals are admitted exactly as in _compare, but counted here
        # so that the arrival stage includes the quick step.
        def arrival_quick(query, _before):
            return engine._compare_arrival_quick(query)
        engine._quick_after_add = arrival_quick
    microbatch_session = None
    if schedule in ('microbatch', 'class_bounded') and quick_sqlwitness:
        import atexit
        from .stream_persistent_microbatch import PersistentOfficialBatch
        microbatch_session = PersistentOfficialBatch(engine, eval_schema, dialect,
                                                     evidence_dir, method_seed)
        atexit.register(microbatch_session.close)
    for q, sql in sqls:
        if engine.remaining() <= 0:
            break
        if not engine.add(q, sql, compare=schedule == 'arrival'):
            break
        if not engine._special_arrival and schedule == 'tail_proof':
            # Default dispatch (10-29 supervision correction): each arrival
            # that hits an existing class gets exactly ONE decision request,
            # against that class's representative. The request is
            # refutation-only, so the equivalence proof stays in the tail;
            # inside _compare the official quick step runs before the polygon
            # search (M4 order). Pairwise comparison of the other in-class
            # members is an ablation and is off by default.
            rep = engine.representatives.get(engine.fingerprints.get(q))
            if rep is not None and rep != q and q not in engine._arrival_requested:
                if engine.no_pool_reuse or not engine._split(q, rep):
                    engine._arrival_requested.add(q)
                    engine._compare(q, rep=rep, refutation_only=True)
                else:
                    # The pool already separates this pair. In the default
                    # configuration that short-circuits the decision; the
                    # pool-reuse ablation starts a fresh pool instead.
                    engine.metrics['cross_pair_reuse'] += 1
            if remainder_quick_comparisons:
                for a, b in engine._quick_board():
                    if engine._split(a, b) or (a, b) in engine._compared:
                        continue
                    engine._compared.add((a, b))
                    engine._compare(a, rep=b, refutation_only=True, use_quick=False)
        if microbatch_session is not None and (len(engine.index[engine.fingerprints[q]]) > 1
                or engine.no_pool_reuse or engine.no_fingerprint_grouping or engine.no_certificate_transfer):
            microbatch_session.run_pending(min_pairs=2)
    if microbatch_session is not None:
        microbatch_session.run_pending()
        microbatch_session.close()
    n = len(engine.arrival)
    within = sum(len(members) * (len(members) - 1) // 2
                 for members in engine.index.values())
    engine.metrics['arrival_separated_pairs'] = n * (n - 1) // 2 - within
    engine.metrics['arrival_stage_wall_s'] = time.perf_counter() - engine.started
    engine.events.append({'event': 'stream_arrivals_complete',
                          'wall_s': engine.metrics['arrival_stage_wall_s'],
                          'processed_queries': n,
                          'separated_pairs': engine.metrics['arrival_separated_pairs']})
    # Arrival-stage counters. The first interval (a quick step between an
    # arrival's admission and the next arrival) is kept apart from remainder
    # comparisons; decision requests are split by the search they invoked.
    stage = engine.metrics
    # stage is an alias of engine.metrics, and the stage view below reuses the
    # name 'fingerprint_separated_pairs' for the arrival pool-hit count. Keep
    # the per-origin library counters before that overwrite.
    live_cats = {'quick': stage['quick_separated_pairs'],
                 'fingerprint': stage['fingerprint_separated_pairs'],
                 'polygon': stage.get('polygon_library_separated_pairs', 0)}
    # Quick calls are split by what issued them: the per-arrival
    # representative request (one decision request per arrival) or the
    # ablation's remainder comparison.
    origin_calls = {}
    for origin in engine._quick_pair_origin.values():
        origin_calls[origin] = origin_calls.get(origin, 0) + 1
    stage['quick_witness_calls_arrival'] = origin_calls.get('arrival', 0)
    stage['quick_witness_calls_remainder'] = origin_calls.get('remainder', 0)
    assert (stage['quick_witness_calls_arrival']
            + stage['quick_witness_calls_remainder']) == stage['quick_witness_calls'], \
        'quick calls without a tracked issuing path'
    if schedule not in ('microbatch', 'class_bounded'):
        assert stage['quick_witness_calls_arrival'] <= len(engine._arrival_requested), \
            'more arrival quick calls than arrival decision requests'
    stage['quick_candidate_pairs'] = getattr(engine, 'quick_candidates', 0)
    stage['pair_decision_requests_through_quick'] = stage['quick_witness_calls']
    stage['quick_arrival_interval_separated_pairs'] = stage.get('quick_arrival_interval_separated_pairs', 0)
    stage['quick_separated_pairs_arrival_interval'] = stage['quick_arrival_interval_separated_pairs']
    stage['quick_separated_pairs_remainder'] = stage['quick_separated_pairs'] - \
        stage['quick_arrival_interval_separated_pairs']
    stage['pair_decision_requests_through_quick'] = stage['quick_witness_calls']
    retry_start = len(engine.events)
    proof_started = time.perf_counter()
    stage['separation_wall_s'] = proof_started - engine.started
    if not separation_only:
        if schedule == 'class_bounded':
            from .stream_class import prove_classes
            try:
                prove_classes(engine, class_off=class_off)
            except BaseException as exc:
                engine._save_replay_context("class_proof_failure",exception=repr(exc))
                raise
        else:
            engine.retry_pending()
            engine.deepen_deferred()
    stage['proof_wall_s'] = time.perf_counter() - proof_started
    if class_off:
        assert all(len(e['receipt']['members']) == 1 for e in engine.events
                   if e.get('event') == 'class_bounded_search')
    # cert_off: replace every label that reaches the representative through an
    # intermediate member by a direct pair decision.
    engine.close_direct_labels()
    if not engine.metrics.get('decision_pool_size'):
        engine.metrics['decision_pool_size'] = len(engine.witnesses)
    partition_wall = time.perf_counter()-engine.started
    stage['tail_candidate_searches'] = sum(
        1 for e in engine.events[retry_start:] if e.get('event') == 'representative_search'
        and e['receipt'].get('status') == 'candidate')
    stage['deferred_deepening_searches'] = sum(1 for e in engine.events[retry_start:]
                                               if e.get('event') == 'representative_search')
    stage['polygon_library_separated_pairs'] = engine.metrics.get('polygon_library_separated_pairs', 0)
    stage['tail_separation_requests'] = stage['pair_decision_requests'] - \
        stage['pair_decision_requests_through_quick'] - stage['deferred_deepening_searches']
    stage['stage_labels'] = {
        'separation_separated_pairs': 'pairs separated within the arrival stage',
        'first_fingerprint_separated_pairs': 'pairs with different fingerprints when the arrival joined',
        'fingerprint_separated_pairs': 'pairs a pool (fingerprint) library is the first to separate',
        'fingerprint_arrival_separated_pairs': 'pairs already separated by the arrival fingerprint',
        'fingerprint_new_class_queries': 'separated queries moved out of their class by one library',
        'quick_separated_pairs': 'pairs separated by an accepted official-quick-step library (all stages)',
        'quick_separated_pairs_arrival_interval': 'separations by quick calls issued between two admissions',
        'quick_separated_pairs_remainder': 'separations by quick calls issued on remainder comparisons',
        'polygon_library_separated_pairs': 'pairs separated by an accepted polygon library (all stages)',
        'quick_witness_calls': 'official SQLWitness quick-step calls (all stages)',
        'quick_witness_calls_arrival': 'quick-step calls issued between two admissions',
        'quick_witness_calls_remainder': 'quick-step calls issued on remainder comparisons',
        'quick_candidate_pairs': 'remainder comparisons handed to the quick step',
        'pair_decision_requests_through_quick': 'remainder pairs that invoked the quick step',
        'pair_decision_requests': 'representative comparisons (all stages)',
        'solver_calls': 'SMT solver checks inside polygon searches',
        'search_separated_pairs': 'separations not attributed to a library or a quick call',
        'seed_pool_separated_pairs': 'separations certified only by the seed pool (reported separately under no_fingerprint_grouping)',
        'library_arrival_fingerprint_overlap_pairs': 'pairs already separated at arrival that an accepted library also separates (reconciled with raw overlap events)',
        'remainder_pair_comparisons': 'pairwise in-class remainder comparisons (ablation switch only, 0 by default)',
        'tail_candidate_searches': 'tail refutation searches',
        'deferred_deepening_searches': 'deferred representative searches',
        'pool_size': 'distinguishing libraries in the pool',
        'decision_pool_size': 'libraries visible to a pair decision (seed count under no_pool_reuse)',
        'cross_pair_reuse': 'pair decisions short-circuited by evidence an earlier pair installed',
        'pool_resets': 'pair decisions that rebuilt the pool from the seed databases',
        'transferred_labels': 'member labels that reach the representative through more than one edge',
        'inherited_labels': 'member observations inherited through a certificate instead of re-executed',
        'augment_rounds': 'multi-bucket target rounds inside representative searches',
        'augment_extra_pairs': 'additional representative pairs a search separated',
        'class_count': 'final classes',
        'separated_pairs_attributed': 'sum of library and quick-call separations'}
    stage['arrival_separated_pairs'] = n * (n - 1) // 2 - sum(
        len(members) * (len(members) - 1) // 2 for members in engine.index.values())
    stage['arrival_separated_pairs_recorded_at_arrival_end'] = engine.metrics['arrival_separated_pairs']
    engine.metrics['arrival_separated_pairs'] = stage['arrival_separated_pairs']
    stage['separation_separated_pairs'] = engine.metrics['arrival_separated_pairs']
    # The arrival counter is cumulative: a seed column that is deactivated
    # later can leave pairs in it that the final partition no longer
    # separates. Recompute every attribution from the final active columns so
    # each final pair is counted once; keep the event total for provenance.
    # Install-time overlap caches describe columns before later arrivals can
    # deactivate them or change their observation mode. Final reporting uses
    # the final active columns, just as the separation attribution below does.
    # Reconcile only after all decisions, then keep every replay invariant.
    engine._recompute_attribution()
    attribution = engine.final_split_attribution()
    stage['arrival_pool_hits_separated_cumulative'] = engine.metrics[
        'arrival_pool_hits_separated']
    engine.metrics['arrival_pool_hits_separated'] = attribution['arrival']
    for _cat in engine.SEPARATION_CATEGORIES:
        engine.metrics[_cat + '_separated_pairs'] = attribution[_cat]
    stage['polygon_library_separated_pairs'] = attribution['polygon']
    stage['fingerprint_separated_pairs'] = attribution['fingerprint']
    stage['quick_separated_pairs'] = attribution['quick']
    stage['fingerprint_arrival_separated_pairs'] = attribution['arrival']
    stage['seed_pool_separated_pairs'] = attribution['seed']
    stage['search_separated_pairs'] = attribution['search']
    stage['fingerprint_new_class_queries'] = attribution['fingerprint']
    stage['separated_pairs_attributed'] = (stage['quick_separated_pairs']
                                           + stage['fingerprint_separated_pairs']
                                           + stage['polygon_library_separated_pairs'])

    assert stage['fingerprint_separated_pairs'] <= stage['arrival_separated_pairs'], \
        'pool separations are part of the separated pairs'
    assert stage['arrival_pool_hits'] == n, 'every accepted arrival must hit the class index'
    engine.evidence_dir = str(Path(evidence_dir).parent) if evidence_dir is not None else None
    engine.assert_invariants(replay=True)
    # Experiment-card switch assertions. Each ablation must leave its
    # mechanism observably off, not merely change a CLI flag.
    if no_fingerprint_grouping:
        assert stage['fingerprint_arrival_separated_pairs'] == 0, \
            'no_fingerprint_grouping still credited arrival index separations'
        assert stage['first_fingerprint_separated_pairs'] == 0, \
            'no_fingerprint_grouping still counted a fingerprint separation'
        assert stage['fingerprint_separated_pairs'] == 0, \
            'no_fingerprint_grouping still attributed a pool separation'
    if no_pool_reuse:
        assert stage['cross_pair_reuse'] == 0, \
            'no_pool_reuse consulted a library installed for another pair'
        assert stage['pool_resets'] >= 1 or stage['pair_decision_requests'] == 0, \
            'no_pool_reuse never rebuilt the pair pool'
    if no_certificate_transfer:
        assert stage['transferred_labels'] == 0, \
            'no_certificate_transfer kept a transitive label'
        assert stage['inherited_labels'] == 0, \
            'no_certificate_transfer inherited a follower observation'
    if single_bucket_target:
        assert stage['augment_rounds'] == 0, \
            'single_bucket_target ran a multi-bucket augmentation round'
        assert stage['augment_extra_pairs'] == 0, \
            'single_bucket_target kept an extra representative pair'
    # Supervision assertion (10-29): a censored batch keeps its certified
    # separations; only unresolved pairs are undecided.
    _snap = engine.snapshot()
    assert _snap['pair_counts']['separated'] == (
        n * (n - 1) // 2 - sum(len(m) * (len(m) - 1) // 2
                               for m in engine.index.values())), \
        'censored batch lost certified separations'
    assert sum(_snap['pair_counts'].values()) == n * (n - 1) // 2
    # Pairs whose fingerprint was separated at arrival are final; the rest are
    # split by accepted libraries or by a search. The first library of a batch
    # also flips the pairs the arrival had already separated, so the library
    # categories alone can exceed the final count; the difference is the
    # overlap between the arrival (first) fingerprint and the accepted
    # libraries, and it is reported rather than asserted away.
    overlap = (stage['quick_separated_pairs'] + stage['fingerprint_separated_pairs']
               + stage['polygon_library_separated_pairs'] - (n * (n - 1) // 2 - within))
    # 'overlap' above is the arithmetic difference once used as a proxy; the
    # reported column is the raw-event count asserted below.
    engine.metrics['remainder_pair_comparisons'] = len(engine._compared)
    stage['remainder_pair_comparisons'] = len(engine._compared)
    assert remainder_quick_comparisons or len(engine._compared) == 0, \
        'default dispatch must not run remainder pair comparisons'
    assert stage['quick_witness_calls_remainder'] == sum(
        1 for o in engine._quick_pair_origin.values() if o == 'remainder'), \
        'remainder quick calls without tracked remainder pairs'
    # The reported overlap must reconcile with the raw events: counted per
    # origin equals tracked raw events, and the independent recomputation from
    # stored observations agrees with the tracked set (assert_invariants).
    # Supervision correction (10-29): the pool-reuse overlap column must be
    # reconciled with the raw events rather than derived from arithmetic.
    # overlap = pairs a library separates that the pool had already separated
    # (arrival fingerprint or an earlier library), counted per library.
    raw_cats, raw_overlap, _ = engine.raw_split_events()
    overlap = engine._lib_overlap_count
    assert overlap == raw_overlap, \
        'counted overlap disagrees with the overlap recomputed from the stored observations'
    # stage['fingerprint_separated_pairs'] is the arrival pool-hit count, not
    # the library category; compare the engine's per-origin counters.
    engine_cats = {'quick': stage['quick_separated_pairs'],
                   'fingerprint': stage['fingerprint_separated_pairs'],
                   'polygon': stage['polygon_library_separated_pairs']}
    assert sum(engine_cats.values()) == sum(raw_cats.values()), \
        'library separation categories disagree with the recomputed first-separations'
    for _cat in ('quick', 'fingerprint', 'polygon'):
        assert engine_cats[_cat] == raw_cats[_cat], \
            '%s separations disagree with the recomputed first-separations' % _cat
    engine.metrics['library_arrival_fingerprint_overlap_pairs'] = overlap
    stage['library_overlap_raw_events'] = raw_overlap
    # Identity: every pair in the final partition has exactly one source — an
    # arrival fingerprint miss, a seed column under the fingerprint ablation,
    # or the first accepted library that separates it. A pair separated by no
    # active column would be unattributed; final_split_attribution() counts
    # that residual directly instead of subtracting cumulative event totals.
    engine.metrics['search_separated_pairs'] = stage['search_separated_pairs']
    assert 0 <= stage['search_separated_pairs'] <= n * (n - 1) // 2, \
        'unattributed separations out of range'
    assert (stage['fingerprint_arrival_separated_pairs']
            + stage['seed_pool_separated_pairs']
            + sum(raw_cats.values())
            + stage['search_separated_pairs']
            == stage['arrival_separated_pairs']), \
        'final partition attribution does not sum to the separated pairs'
    if prover is not None:
        prover.close()
        engine.metrics['native_prover_worker_starts'] = prover.session.starts
    if quick_session is not None:
        quick_session.close()
        engine.metrics['quick_witness_worker_starts'] = quick_session.starts
    result = engine.snapshot()
    result['unprocessed_query_ids'] = [q for q, _ in sqls[len(engine.arrival):]]
    result['metrics']['input_queries'] = len(sqls)
    result['metrics']['processed_queries'] = len(engine.arrival)
    result['metrics']['unprocessed_queries'] = len(result['unprocessed_query_ids'])
    result['metrics']['partition_wall_s'] = partition_wall
    result['metrics']['throughput_queries_s'] = len(engine.arrival)/result['metrics']['wall_s']
    result['tiers'] = {'separation_wall_s':stage['separation_wall_s'],
                       'proof_wall_s':stage['proof_wall_s'],
                       'finalization_wall_s':max(0.,result['metrics']['wall_s']-stage['separation_wall_s']-stage['proof_wall_s'])}
    if schedule == 'class_bounded':
        from .class_metrics import depth_counts
        result['class_depth_counts'] = depth_counts(engine.events, stage.get('class_checks', 0))
        result['class_off'] = class_off
    result['resource_scope'] = 'host process and waited child CPU; external DBMS CPU excluded; peak_rss_kib is parent only'
    return engine, result


def run_sqlwitness(schema, eval_schema, queries, dialect, backend, budget_s, seed,
                   bounds=None, random_dbs=2, shell='all', order='source',start_time=None,
                   method_seed=0, tool='sqlwitness', native_session_dir=None):
    if shell not in ('all', 'representative'):
        raise ValueError('unknown SQLWitness shell')
    if tool not in ('sqlwitness', 'parseval', 'xdatan', 'sqldriller_official'):
        raise ValueError('unknown released pair tool')
    if tool != 'sqlwitness' and shell != 'all':
        raise ValueError('representative shell is reserved for SQLWitness')
    session = (PersistentPairTool(tool, native_session_dir) if tool in ('sqlwitness', 'parseval')
               and native_session_dir is not None else None)
    def call_pair(a, b, budget, round_no, call_id):
        if tool == 'sqlwitness':
            return native_sqlwitness(eval_schema, dialect, a, b, budget,
                                     method_seed+1000*round_no, call_id, session=session)
        stages = ['testsuite'] if tool == 'sqldriller_official' and round_no > 0 else None
        return native_pair_tool(tool, eval_schema, dialect, a, b, budget,
                                method_seed+1000*round_no, call_id, stages=stages,
                                session=session)
    sqls = list(queries)
    if order == 'reverse':
        sqls.reverse()
    elif order == 'shuffle':
        random.Random(seed).shuffle(sqls)
    elif order != 'source':
        raise ValueError('unknown arrival order')
    engine = StreamPartition(schema, dialect, backend, budget_s=budget_s, seed=method_seed,
                             random_dbs=random_dbs, bounds=bounds,start_time=start_time)
    calls, closed = [], set()
    if shell == 'representative':
        for q, sql in sqls:
            if engine.remaining() <= 0:
                break
            if not engine.add(q, sql, compare=False):
                break
            fp = engine.fingerprints[q]
            rep = engine.representatives[fp]
            if rep == q or engine.remaining() <= 0:
                continue
            budget = min(60, engine.remaining())
            call_deadline = min(engine.deadline, time.perf_counter() + budget)
            phase_start = time.perf_counter()
            outcome = call_pair(engine.queries[q], engine.queries[rep], budget, 0,
                                f'representative-{len(calls)}')
            engine.metrics['refutation_search_s'] += time.perf_counter()-phase_start
            record = {'a': q, 'b': rep, 'round': 0, 'native': outcome['native'],
                      'wall_s': time.perf_counter()-engine.started}
            calls.append(record)
            if outcome['native']['verdict'] in ('unsupported', 'error'):
                closed.add((q, rep))
            if outcome['database'] is not None:
                record['engine_admitted'] = engine._install(outcome['database'], target=(q, rep),
                                                            admission_deadline=call_deadline)
    else:
        # Exactly the same seed pools, query convention, engine and result keys.
        for q, sql in sqls:
            if engine.remaining() <= 0:
                break
            if not engine.add(q, sql, compare=False):
                break
    pairs = list(itertools.combinations(engine.arrival, 2)) if shell == 'all' else []
    round_no = 1 if shell == 'representative' else 0
    while engine.remaining() > 0:
        if shell == 'representative':
            targets = [(q, engine.representatives[engine.fingerprints[q]]) for q in engine.arrival]
            targets = [(a, b) for a, b in targets if a != b and (a, b) not in closed]
        else:
            targets = [(a, b) for a, b in pairs if (a, b) not in closed
                       and engine.fingerprints[a] == engine.fingerprints[b]]
        if not targets:
            break
        for a, b in targets:
            if engine.remaining() <= 0:
                break
            if a == b or engine.fingerprints[a] != engine.fingerprints[b]:
                continue
            budget = min(60, engine.remaining())
            call_deadline = min(engine.deadline, time.perf_counter() + budget)
            phase_start = time.perf_counter()
            outcome = call_pair(engine.queries[a], engine.queries[b], budget, round_no,
                                f'{shell}-{len(calls)}')
            engine.metrics['refutation_search_s'] += time.perf_counter()-phase_start
            native = outcome['native']
            record = {'a': a, 'b': b, 'round': round_no, 'native': native,
                      'wall_s': time.perf_counter()-engine.started}
            calls.append(record)
            if tool == 'parseval':
                # Released disprove is one attempt per pair in its native batch.
                closed.add((a, b))
            if native['verdict'] in ('unsupported', 'error') or (tool == 'sqldriller_official'
                                                            and native.get('stage') == 'sqlsolver_eq'):
                closed.add((a, b))
            if outcome['database'] is not None:
                record['engine_admitted'] = engine._install(outcome['database'], target=(a, b),
                                                            admission_deadline=call_deadline)
        round_no += 1
    partition_wall = time.perf_counter()-engine.started
    engine.assert_invariants(replay=True)
    if session is not None:
        session.close()
        engine.metrics['native_worker_starts'] = session.starts
    result = engine.snapshot()
    result['unprocessed_query_ids'] = [q for q, _ in sqls[len(engine.arrival):]]
    result['metrics']['input_queries'] = len(sqls)
    result['metrics']['processed_queries'] = len(engine.arrival)
    result['metrics']['unprocessed_queries'] = len(result['unprocessed_query_ids'])
    result['shell'] = shell
    result['tool'] = tool
    result['calls'] = calls
    result['metrics']['native_pair_calls'] = len(calls)
    result['metrics']['pair_decision_requests'] = len(calls)
    result['metrics']['solver_calls'] = 0
    result['native_solver_coverage'] = f'{tool} native solver calls are opaque; released pair-tool calls are reported separately'
    result['metrics']['partition_wall_s'] = partition_wall
    result['metrics']['throughput_queries_s'] = len(engine.arrival)/result['metrics']['wall_s']
    result['resource_scope'] = 'host process and waited child CPU; external DBMS CPU excluded; peak_rss_kib is parent only'
    return engine, result


def run_xdatan_multi(schema, eval_schema, queries, dialect, backend, budget_s, seed,
                     order='source', start_time=None, method_seed=0, random_dbs=2):
    """Released one-reference/many-mutants API with shared Σ and DBMS admission."""
    from seereval import tools
    from seereval.engine import _coerce
    from seer2.backends import EngineRejected

    sqls = list(queries)
    if order == 'reverse':
        sqls.reverse()
    elif order == 'shuffle':
        random.Random(seed).shuffle(sqls)
    elif order != 'source':
        raise ValueError('unknown arrival order')
    engine = StreamPartition(schema, dialect, backend, budget_s=budget_s,
                             seed=method_seed, random_dbs=random_dbs, start_time=start_time)
    for q, sql in sqls:
        if engine.remaining() <= 0 or not engine.add(q, sql, compare=False):
            break
    calls, tried = [], set()
    while engine.remaining() > 0:
        rep = next((q for q in engine.arrival if q not in tried and
                    any(engine.fingerprints[q] == engine.fingerprints[other]
                        for other in engine.arrival if other != q)), None)
        if rep is None:
            break
        mutants = [q for q in engine.arrival if q != rep and
                   engine.fingerprints[q] == engine.fingerprints[rep]]
        tried.add(rep)
        budget = min(60.0, engine.remaining())
        deadline = min(engine.deadline, time.perf_counter() + budget)
        request = tools.build_request('xdatan', eval_schema,
                                     'postgresql' if dialect == 'postgres' else dialect,
                                     [engine.queries[q] for q in [rep, *mutants]],
                                     budget, method_seed)
        if os.environ.get("SEER_ENTRY_METRICS"):
            request.update(_entry_metric_tool="xdatan", _entry_metric_call_id=f"batch-{len(calls)}")
        phase_start = time.perf_counter()
        native = tools.call('xdatan', request, hard_s=budget)
        engine.metrics['refutation_search_s'] += time.perf_counter() - phase_start
        record = {'reference': rep, 'mutants': mutants,
                  'native': {k: v for k, v in native.items() if k != 'instances'},
                  'datasets': [], 'wall_s': time.perf_counter() - engine.started}
        calls.append(record)
        for original in native.get('instances') or []:
            if engine.remaining() <= 0:
                break
            try:
                database = {}
                for table in eval_schema.tables:
                    rows = next((v for name, v in original.items()
                                 if name.lower() == table.name.lower()), [])
                    database[table.name.lower()] = []
                    for row in rows:
                        low = {str(k).lower(): v for k, v in row.items()}
                        database[table.name.lower()].append({
                            c['name'].lower(): _coerce(low.get(c['name'].lower()), c['type'])
                            for c in table.columns})
                if schema.violations(database):
                    record['datasets'].append({'admitted': False, 'reason': 'sigma'})
                    continue
                observations = engine._run(database, [rep, *mutants], phase='replay_s')
                target = next((q for q in mutants if observations[rep].stable and
                               observations[q].stable and
                               observations[rep].key != observations[q].key), None)
                if target is None:
                    record['datasets'].append({'admitted': False,
                                               'reason': 'no_stable_reference_split'})
                    continue
                admitted = engine._install(database, target=(rep, target),
                                            admission_deadline=deadline)
                record['datasets'].append({'admitted': admitted, 'target': target})
            except (EngineRejected, ValueError, TypeError) as exc:
                record['datasets'].append({'admitted': False,
                                           'reason': type(exc).__name__})
    partition_wall = time.perf_counter() - engine.started
    engine.assert_invariants(replay=True)
    result = engine.snapshot()
    result['unprocessed_query_ids'] = [q for q, _ in sqls[len(engine.arrival):]]
    result['metrics']['input_queries'] = len(sqls)
    result['metrics']['processed_queries'] = len(engine.arrival)
    result['metrics']['unprocessed_queries'] = len(result['unprocessed_query_ids'])
    result['shell'] = 'all'
    result['tool'] = 'xdatan'
    result['native_mode'] = 'xdatan_one_reference_many_mutants'
    result['calls'] = calls
    result['metrics']['native_batch_calls'] = len(calls)
    result['metrics']['native_pair_calls'] = sum(len(c['mutants']) for c in calls)
    result['metrics']['pair_decision_requests'] = result['metrics']['native_pair_calls']
    result['metrics']['solver_calls'] = 0
    result['native_solver_coverage'] = 'XDataN Java-internal solver calls are opaque'
    result['metrics']['partition_wall_s'] = partition_wall
    result['metrics']['throughput_queries_s'] = len(engine.arrival) / result['metrics']['wall_s']
    result['resource_scope'] = 'host process and waited child CPU; external DBMS CPU excluded; peak_rss_kib is parent only'
    return engine, result
