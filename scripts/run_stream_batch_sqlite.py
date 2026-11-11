"""One immutable batch run; reused by acceptance and later scaling cohorts."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
TMPDIR = os.environ.get("TMPDIR", "/tmp")
import argparse
import hashlib
import json
import os
import resource
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'eval'),str(ROOT/'scripts')]
from seereval.schema import Schema as EvalSchema, to_ddl
from seereval.engine import normalize_sql
from seer2.schema import Schema
from seer.core import _jsonable
from seer.stream_runner import run_stream, run_sqlwitness, run_xdatan_multi
from seer.official_batch import run_official_batch
from seer_sqlwitness import sigma_extra, DIALECT, BACKEND

def setup_meter(output, method):
    sys.path.insert(0, str(ROOT/'eval/tools'))
    import entry_metrics as meter
    os.environ['SEER_ENTRY_METRICS'] = str(output/'entry-metrics.jsonl')
    meter.configure(os.environ['SEER_ENTRY_METRICS'], 'host:'+method)
    meter.install_solver()
    from seer2 import backends
    for name in ('_DuckInst','_SqliteInst','_PgInst','_MyInst'):
        meter.wrap(getattr(backends,name),'run','source_query_execute_attempt')
    for name in ('DuckDBBackend','SQLiteBackend','PostgresBackend','MySQLBackend'):
        meter.wrap(getattr(backends,name),'open','source_database_load_attempt')
    meter.record('coverage', component='host', python_solver=True, source_queries=True)

def cgroup_resources():
    cg=next(line.split('::',1)[1] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
    root=Path('/sys/fs/cgroup')/cg.lstrip('/')
    return {'cgroup':cg, 'memory_peak_bytes':(root/'memory.peak').read_text().strip(),
            'cpu_stat':(root/'cpu.stat').read_text(),
            'scope':'whole dedicated service including host/native children, excluding external DBMS'}


def sqlite_versions():
    official = subprocess.check_output([
        (SEER_ROOT + "/repro/sqlwitness/venv-sw-py311-pinned/bin/python"),
        "-c", "import sqlite3; print(sqlite3.sqlite_version)"], text=True).strip()
    return {"host_replay":sqlite3.sqlite_version, "official_driver":official,
            "precheck":"host_replay EXPLAIN common accepted input set"}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def emit_missing_schema_batch(a, source, batch, batch_start, cpu_start):
    """Keep an original-panel batch with unsupported source input in the denominator."""
    reason = batch.get('meta', {}).get('unsupported_reason')
    if reason not in ('original_and_normalized_schema_missing',
                      'unsupported_original_statement', 'unmapped_original_schema',
                      'sqlite_input_unsupported'):
        raise ValueError('schema_path missing without an explicit source reason')
    from seer.stream import CONTRACT
    qids = [q['qid'] for q in batch['queries']]
    if len(qids) != len(set(qids)):
        raise ValueError('duplicate query identities in unsupported batch')
    code_files = list((ROOT/'src/seer').glob('*.py')) + list((ROOT/'src/seer2').glob('*.py'))
    code_files += [Path(__file__), ROOT / 'scripts/sqlwitness_batch_worker.py',
                   ROOT / 'scripts/prepare_sqlite_panels.py', source]
    config = {
        'method': a.method, 'ablation': a.ablation, 'batch': a.batch,
        'seed': a.seed, 'order': a.order, 'budget_s': a.budget,
        'per_call_s': a.per_call_s, 'original_pair_timeout_s': a.original_pair_timeout_s,
        'source_commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'files': {str(path): sha(path) for path in code_files},
        'query_ids': qids, 'unsupported_reason': reason,
        'dialect': 'sqlite', 'backend': 'sqlite', 'sqlite_versions': sqlite_versions(),
        'db_endpoint': {'engine':'sqlite','mode':'in_process_memory'},
        'sqlite_conversion': batch.get('meta',{}).get('sqlite_conversion'),
        'affinity': sorted(os.sched_getaffinity(0)),
        'temporary_directory': str(Path(os.environ['TMPDIR']).resolve()),
    }
    expected_tmp = Path((TMPDIR))
    assert Path(os.environ['TMPDIR']).resolve() == expected_tmp
    assert Path(os.environ['SQLITE_TMPDIR']).resolve() == expected_tmp
    assert Path(os.environ['SEER_SQLWITNESS_SQLITE_SCRATCH']).resolve() == expected_tmp
    (a.out/'config.json').write_text(json.dumps(config, indent=2)+'\n')
    setup_meter(a.out, a.method)
    n = len(qids)
    total_pairs = n * (n-1) // 2
    wall = time.perf_counter()-batch_start
    cpu_end = sum((r.ru_utime+r.ru_stime) for r in
                  (resource.getrusage(resource.RUSAGE_SELF),resource.getrusage(resource.RUSAGE_CHILDREN)))
    result = {
        'contract': CONTRACT, 'classes': [qids], 'representatives': qids[:1],
        'relations': {qid: {'status': 'unsupported', 'reason': reason,
                            'evidence_edges': []} for qid in qids},
        'class_sizes': [n], 'class_size_histogram': {str(n): 1}, 'class_count': 1,
        'pair_counts': {'separated': 0, 'proved': 0, 'bounded': 0,
                        'undecided': total_pairs},
        'member_status_counts': {'unsupported': n}, 'domain_identity': None,
        'evidence_edges': [], 'events': [], 'witnesses': [],
        'unprocessed_query_ids': [], 'entry_counts': {'host:'+a.method+':coverage': 1},
        'unsupported_reason': reason,
        'unsupported_query_ids': qids, 'original_reference_pairs': n-1,
        'original_all_pairs': total_pairs, 'unsupported_all_pairs': total_pairs,
        'metrics': {
            'input_queries': n, 'processed_queries': n, 'unprocessed_queries': 0,
            'unsupported_queries': n, 'unsupported_reference_pairs': n-1,
            'invalid_admitted_certificates': 0, 'solver_calls': 0,
            'executed_queries':0, 'native_pair_calls':0, 'quick_witness_calls':0,
            'pair_decision_requests':0,
            'small_db_executions': 0, 'pool_size': 0, 'active_pool_size': 0,
            'encoding_s': 0., 'pool_execution_s': 0., 'refutation_search_s': 0.,
            'bounded_proof_s': 0., 'replay_s': 0., 'wall_s': wall,
            'cpu_s': cpu_end-cpu_start,
            'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'throughput_queries_s': n/wall if wall else 0.,
            'completed_within_budget': wall <= a.budget,
            'censored_at_budget': wall > a.budget,
        },
        'cgroup_resources': cgroup_resources(),
        'uptime_end': Path('/proc/uptime').read_text().strip(),
        'config_sha256': sha(a.out/'config.json'),
        'timing_scope': 'source read and explicit unsupported classification; no DBMS or solver work',
    }
    assert all(sha(path)==digest for path,digest in config['files'].items())
    assert result['metrics']['invalid_admitted_certificates'] == 0
    assert sum(result['pair_counts'].values()) == total_pairs
    (a.out/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({'finished': a.batch, 'unsupported_reason': reason,
                      'queries': n, 'reference_pairs': n-1}), flush=True)


def restore_input_denominator(result, original_qids, unsupported, conversion, executable_query_count):
    # The execution engine only sees the common executable subset. Restore the
    # original input denominator without assigning rejected queries to any
    # equivalence class or manufacturing an equality/inequality decision.
    result['unsupported_query_ids'] = [q for q in original_qids if q in unsupported]
    arrived = result['metrics']['processed_queries']
    assert sum(result['pair_counts'].values()) == arrived*(arrived-1)//2
    result['metrics']['executed_queries'] = arrived
    result['metrics']['processed_queries'] += len(unsupported)
    result['metrics']['input_queries'] = len(original_qids)
    result['metrics']['unsupported_queries'] = len(unsupported)
    result['metrics']['unsupported_reference_pairs'] = conversion['unsupported_reference_pairs']
    result['original_reference_pairs'] = conversion['original_reference_pairs']
    result['original_all_pairs'] = len(original_qids)*(len(original_qids)-1)//2
    result['unsupported_all_pairs'] = result['original_all_pairs'] - executable_query_count*(executable_query_count-1)//2
    result['unprocessed_all_pairs'] = (executable_query_count*(executable_query_count-1)
                                       - arrived*(arrived-1))//2
    result['pair_counts']['undecided'] += (result['unsupported_all_pairs']
                                          + result['unprocessed_all_pairs'])
    for qid in unsupported:
        result['relations'][qid] = {'status':'unsupported',
            'reason':conversion['unsupported_reasons'][qid], 'evidence_edges':[]}
    result['member_status_counts']['unsupported'] = len(unsupported)
    assert sum(result['pair_counts'].values()) == result['original_all_pairs']


def resolve_budget(a, batch):
    """Map a published pair deadline onto one same-reference stream batch.

    The fixed panel has one reference and all original candidates, so its
    cumulative pair allowance is the per-pair deadline times the unfiltered
    number of original candidates. A per-decision cap keeps one representative
    search from consuming another pair's allowance.
    """
    if a.original_pair_timeout_s is None:
        a.budget = 600. if a.budget is None else a.budget
        a.per_call_s = 60.
        return
    if a.budget is not None:
        raise ValueError('give either a batch budget or the original pair deadline')
    if a.method not in ('seer_stream', 'seer_stream_sqlwitness'):
        raise ValueError('original pair deadline is for the SEER same-reference panel')
    if batch.get('dataset') not in (
            'leetcode_verieql_original_batches',
            'xdatabm',
            'calcite_verieql_paper_original',
            'literature_verieql_paper_original',
            'spider_sqlwitness_original_batches',
            'bird_sqlwitness_original_batches',
            'bird_parseval_original_batches'):
        raise ValueError('original pair deadline requires a frozen published panel')
    queries = batch['queries']
    reference = [q for q in queries if q.get('role') == 'reference']
    candidates = [q for q in queries if q.get('role') == 'candidate']
    count = len(candidates)
    declared = batch.get('meta', {}).get('original_pair_count')
    if len(reference) != 1 or len(queries) != count + 1 or (
            declared is not None and declared != count):
        raise ValueError('original panel reference/candidate denominator changed')
    a.original_pair_count = count
    a.per_call_s = a.original_pair_timeout_s
    a.budget = count * a.per_call_s


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--dataset',required=True);ap.add_argument('--batch',required=True)
    ap.add_argument('--method',choices=('seer_stream','seer_stream_sqlwitness',
                                       'sqlwitness','sqlwitness_representative',
                                       'parseval','xdatan','sqldriller_official'),required=True)
    ap.add_argument('--ablation',choices=('none','no_prover','random_representative',
                                         'no_fingerprint_grouping','no_pool_reuse'),default='none')
    ap.add_argument('--budget',type=float,default=None);ap.add_argument('--seed',type=int,default=0)
    ap.add_argument('--original-pair-timeout-s',type=float,choices=(60.,120.,360.,600.),default=None)
    ap.add_argument('--order',choices=('source','reverse','shuffle'),default='source')
    ap.add_argument('--schedule',choices=('arrival','tail_proof','microbatch','class_bounded'),default='tail_proof')
    ap.add_argument('--separation-only',action='store_true')
    ap.add_argument('--validate-class-bounded',action='store_true')
    ap.add_argument('--quick-schedule',choices=('arrival','compare'),default='arrival',
                    help='compare: the 10-28 successor configuration')
    ap.add_argument('--no-certificate-transfer',action='store_true',
                    help='ablation: followers are re-executed instead of inheriting a certified edge')
    ap.add_argument('--single-bucket-target',action='store_true',
                    help='ablation: one distinguishing bucket per search (no multi-bucket target)')
    ap.add_argument('--remainder-quick-comparisons',action='store_true',
                    help='ablation only: pairwise in-class remainder comparisons '
                         '(off by default per the 10-29 supervision correction)')
    ap.add_argument('--candidate-budget-s',type=float,default=0.5)
    ap.add_argument('--quick-failure-cap',type=int,default=0,
                    help='0 keeps the official quick step available for every class hit')
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--native-runtime',type=Path,default=ROOT.parent/'repro/sqlsolver-artifact-blobs-v1/runtime-link-check-v1/runtime.json')
    a=ap.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    # Retain uncaught Python and native-child diagnostics with this receipt.
    # The launcher may keep a separate scheduling log; neither stream is lost.
    for fd, name in ((1, 'stdout.log'), (2, 'stderr.log')):
        sink = os.open(a.out/name, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600)
        os.dup2(sink, fd)
        os.close(sink)

    batch_start=time.perf_counter()
    cpu_start=sum((r.ru_utime+r.ru_stime) for r in
                  (resource.getrusage(resource.RUSAGE_SELF),resource.getrusage(resource.RUSAGE_CHILDREN)))
    source=Path(a.dataset).resolve()
    selected=[json.loads(line) for line in source.open() if json.loads(line)['batch_id']==a.batch]
    if len(selected)!=1:
        raise ValueError('expected exactly one batch')
    b=selected[0]
    resolve_budget(a, b)
    conversion = b.get('meta', {}).get('sqlite_conversion')
    if not conversion or b['dialect'] != 'sqlite':
        raise ValueError('requires frozen prepare_sqlite_panels input')
    assert sha(ROOT/'scripts/prepare_sqlite_panels.py') == conversion['rules_sha256'], 'conversion rules changed'
    unsupported = set(conversion['unsupported_qids'])
    original_qids = [q['qid'] for q in b['queries']]
    assert len(original_qids) == conversion['original_query_count']
    if len(unsupported) == len(original_qids):
        b['meta']['unsupported_reason'] = 'sqlite_input_unsupported'
        emit_missing_schema_batch(a, source, b, batch_start, cpu_start)
        return
    if b.get('schema_path') is None:
        emit_missing_schema_batch(a, source, b, batch_start, cpu_start)
        return
    schema_path=(source.parent/b['schema_path']).resolve()
    es=EvalSchema.load(schema_path)
    dialect=DIALECT[b['dialect']];backend=BACKEND[dialect]
    schema=Schema(';\n'.join(es.native['sqlite_panel']['ddl'])+';',dialect,inclusions=sigma_extra(es))
    qs=[(q['qid'],normalize_sql(q['sql'],b['dialect'])) for q in b['queries'] if q['qid'] not in unsupported]
    files=list((ROOT/'src/seer').glob('*.py'))+list((ROOT/'src/seer2').glob('*.py'))
    files+=list((ROOT/'scripts/java').glob('*.java'))
    files+=list((ROOT.parent/'venv-sw/lib/python3.10/site-packages/sqlwitness').rglob('*.py'))
    files+=list((ROOT/'eval/tools').glob('*.py'))+list((ROOT/'eval/seereval').glob('*.py'))+[Path(__file__),ROOT/'scripts/sqlwitness_batch_worker.py',source,schema_path,a.native_runtime,ROOT/'scripts/prepare_sqlite_panels.py']
    config={'method':a.method,'ablation':a.ablation,'batch':a.batch,'seed':a.seed,'order':a.order,
            'method_seed':0,'randomness_contract':'seed changes arrival permutation only','budget_s':a.budget,
            'per_call_s':a.per_call_s,'original_pair_timeout_s':a.original_pair_timeout_s,
            'original_reference_pairs':getattr(a, 'original_pair_count', b.get('meta',{}).get('original_pair_count')),
            'initial_random_dbs':2,'bounds':'1,2,4,... until call budget',
            'affinity':sorted(os.sched_getaffinity(0)), 'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'files':{str(p):sha(p) for p in files},'query_ids':original_qids,
            'executed_query_ids':[q for q,_ in qs], 'dialect':dialect, 'backend':backend,
            'sqlite_versions':sqlite_versions(),
            'db_endpoint':{'engine':'sqlite','mode':'in_process_memory'},
            'sqlite_conversion':conversion,
            'uptime_start':Path('/proc/uptime').read_text().strip()}
    if a.method in ('seer_stream','seer_stream_sqlwitness'):
        config['stream_schedule'] = a.schedule
        config['separation_only'] = a.separation_only
        config['candidate_budget_s'] = a.candidate_budget_s
        config['quick_failure_cap'] = (a.quick_failure_cap or None) if a.method=='seer_stream_sqlwitness' else None
        config['quick_schedule'] = a.quick_schedule
        config['remainder_quick_comparisons'] = a.remainder_quick_comparisons
        config['no_certificate_transfer'] = a.no_certificate_transfer
        config['single_bucket_target'] = a.single_bucket_target
        config['no_fingerprint_grouping'] = a.ablation == 'no_fingerprint_grouping'
        config['no_pool_reuse'] = a.ablation == 'no_pool_reuse'
    if a.method == 'sqlwitness':
        from seer.official_batch import ARTIFACT
        config['native_mode'] = 'official_experiment_driver_one_process_per_batch'
        config['official_driver_sha256'] = sha(ARTIFACT/'experiments/run_sqlwitness.py')
        config['official_benchmark_loader_sha256'] = sha(ARTIFACT/'experiments/benchmarks.py')
    if dialect == "mysql":
        primary = urlparse(os.environ["SEER_MYSQL_URL"])
        official = urlparse(os.environ["SQLWITNESS_MYSQL_URL"])
        endpoint = (primary.hostname, primary.port)
        expected_port = int(os.environ.get('SEER_EXPECTED_MYSQL_PORT', '23308'))
        assert expected_port in (23306, 23307, 23308)
        assert endpoint == (official.hostname, official.port) == ("127.0.0.1", expected_port)
        config["db_endpoint"] = {"engine": "mysql", "host": endpoint[0], "port": endpoint[1]}
    cgroup = [line.split('::', 1)[1] for line in Path('/proc/self/cgroup').read_text().splitlines()
              if line.startswith('0::')]
    # systemd-run without --scope redirects the launcher into its transient
    # service, so the owning unit is the nearest *.service path component.
    units = [part for part in (cgroup[0].strip().split('/') if cgroup else []) if part.endswith('.service')]
    if units:
        config['service_unit'] = units[-1]
    if os.environ.get('INVOCATION_ID'):
        config['service_invocation_id'] = os.environ['INVOCATION_ID']
    expected_tmp = Path((TMPDIR))
    assert Path(os.environ.get("TMPDIR", "")).resolve() == expected_tmp
    assert Path(os.environ.get("SEER_SQLWITNESS_SQLITE_SCRATCH", "")).resolve() == expected_tmp
    assert Path(os.environ.get("SQLITE_TMPDIR", "")).resolve() == expected_tmp
    assert Path(tempfile.gettempdir()).resolve() == expected_tmp
    config["temporary_directory"] = str(expected_tmp)
    (a.out/'config.json').write_text(json.dumps(config,indent=2)+'\n')
    print(json.dumps({'start':a.batch,'method':a.method,'queries':len(qs),'affinity':config['affinity']}),flush=True)
    setup_meter(a.out, a.method)
    kwargs=dict(schema=schema,queries=qs,dialect=dialect,backend=backend,budget_s=a.budget,
                seed=a.seed,method_seed=0,start_time=batch_start)
    if a.method in ('seer_stream','seer_stream_sqlwitness'):
        engine,result=run_stream(**kwargs,native_runtime=a.native_runtime,evidence_dir=a.out/'native',
                                 order=a.order,eval_schema=es,
                                 quick_sqlwitness=a.method=='seer_stream_sqlwitness',
                                 enable_prover=a.ablation!='no_prover',
                                 random_representative=a.ablation=='random_representative',
                                 no_fingerprint_grouping=a.ablation=='no_fingerprint_grouping',
                                 no_pool_reuse=a.ablation=='no_pool_reuse',schedule=a.schedule,
                                 candidate_budget_s=a.candidate_budget_s,
                                 quick_failure_cap=a.quick_failure_cap or None,
                                 separation_only=a.separation_only,
                                 per_call_s=a.per_call_s,
                                 quick_schedule=(a.quick_schedule if a.method=='seer_stream_sqlwitness'
                                                 else 'compare'),
                                 remainder_quick_comparisons=(a.remainder_quick_comparisons
                                                              and a.method=='seer_stream_sqlwitness'),
                                 no_certificate_transfer=a.no_certificate_transfer,
                                 single_bucket_target=a.single_bucket_target)
    elif a.method == 'sqlwitness':
        engine,result=run_official_batch(**kwargs,eval_schema=es,order=a.order,
                                         output_dir=a.out)
    elif a.method == 'xdatan':
        engine,result=run_xdatan_multi(**kwargs,eval_schema=es,order=a.order)
    else:
        engine,result=run_sqlwitness(**kwargs,eval_schema=es,order=a.order,
                                     shell='representative' if a.method=='sqlwitness_representative' else 'all',
                                     tool='sqlwitness' if a.method=='sqlwitness_representative' else a.method,
                                     native_session_dir=a.out/'native-session')
    origins = getattr(engine, '_witness_origin', {})
    result['witnesses']=[{'database':_jsonable(w.database),'domain':w.domain,'k':w.k,
                           'active':w.active,'reason':w.reason,'mode':w.mode,
                           'origin':origins.get(id(w))} for w in engine.witnesses]
    from collections import Counter
    entries=[json.loads(line) for line in (a.out/'entry-metrics.jsonl').read_text().splitlines()]
    result['entry_counts'] = dict(Counter((r['role']+':'+r['kind']) for r in entries))
    assert result['entry_counts'].get('host:'+a.method+':source_query_execute_attempt',0) == result['metrics']['small_db_executions']
    observed_checks = result['entry_counts'].get('host:'+a.method+':python_solver_check_attempt',0)
    reported_checks = result['metrics']['solver_calls']
    assert observed_checks >= reported_checks, 'instrumented solver attempts cannot be fewer than completed calls'
    result['metrics']['solver_calls_engine_reported'] = reported_checks
    result['metrics']['solver_calls'] = observed_checks
    result['cgroup_resources'] = cgroup_resources()
    result['uptime_end']=Path('/proc/uptime').read_text().strip()
    result['config_sha256']=sha(a.out/'config.json')
    assert all(sha(p)==h for p,h in config['files'].items()),'source changed during run'
    if a.method == 'sqlwitness':
        from seer.official_batch import ARTIFACT
        assert sha(ARTIFACT/'experiments/run_sqlwitness.py') == config['official_driver_sha256']
        assert sha(ARTIFACT/'experiments/benchmarks.py') == config['official_benchmark_loader_sha256']
    restore_input_denominator(result, original_qids, unsupported, conversion, len(qs))
    result['metrics']['engine_wall_s']=result['metrics']['wall_s']
    result['metrics']['wall_s']=time.perf_counter()-batch_start
    result['metrics']['throughput_queries_s']=result['metrics']['processed_queries']/result['metrics']['wall_s']
    result['metrics']['completed_within_budget']=(result['metrics']['unprocessed_queries']==0
                                                   and result['metrics']['wall_s']<=a.budget)
    result['metrics']['censored_at_budget']=not result['metrics']['completed_within_budget']
    cpu_end=sum((r.ru_utime+r.ru_stime) for r in
                (resource.getrusage(resource.RUSAGE_SELF),resource.getrusage(resource.RUSAGE_CHILDREN)))
    result['metrics']['cpu_s']=cpu_end-cpu_start
    result['timing_scope']='first source batch read through verified partition and result preparation; includes normalization, encoding, witness pool execution, solver work, replay, and native tool overhead'
    result['phase_timing_scope']={
        'encoding_s':'Prepared plus front-end translation and domain update',
        'pool_execution_s':'host DBMS executions on pool or proposed witness databases',
        'refutation_search_s':'Polygon candidate search or official SQLWitness call',
        'bounded_proof_s':'native SQLSolver plus Polygon no-counterexample search',
        'replay_s':'fresh target replay, independent probes, and final invariant replay',
        'note':'phase wall times exclude setup, index maintenance, result serialization, and caller overhead; total wall time includes them'}
    if a.validate_class_bounded:
        assert a.schedule == 'class_bounded'
        from seer.class_validation import verify_bounded
        validation_started=time.perf_counter()
        try:
            result['class_assertions']=verify_bounded(engine,seconds=a.per_call_s)
        except BaseException as exc:
            engine._save_replay_context('class_bounded_assertion',exception=repr(exc))
            raise
        result['class_assertions']['wall_s']=time.perf_counter()-validation_started
        result['class_assertions']['passed']=True
        result['development_only']=True
    (a.out/'result.json').write_text(json.dumps(result,default=str,indent=2)+'\n')
    print(json.dumps({'finished':a.batch,'metrics':result['metrics']}),flush=True)


if __name__=='__main__':
    main()
