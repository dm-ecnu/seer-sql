"""Score the frozen LeetCode stream cohort only after every test unit exists.

Two SEER configurations come from the fixed v10-final cohort; the released SQLWitness all-pair and
representative shells come from the contemporaneous v9 matrix. Development
batches are excluded by the frozen test.ids file.
"""
from __future__ import annotations

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
TMPDIR = os.environ.get("TMPDIR", "/tmp")
import argparse
import csv
import hashlib
import io
import json
import math
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

BASE = Path((SEER_ROOT))
DATA = BASE / 'datasets/batches/leetcode-scale-v1'
RESULTS = BASE / 'repro/stream-partition-v1'
NEW = RESULTS / 'leetcode-scale-v10-final'
OLD = RESULTS / 'leetcode-scale-v9'
SUPPLEMENT = RESULTS / 'leetcode-scale-v9-supplement-v1'
SOURCES = {
    'seer_stream': (NEW, 'b1c229471630e2e3a178fcca53ccec2ba0ac988f'),
    'seer_stream_sqlwitness': (NEW, 'b1c229471630e2e3a178fcca53ccec2ba0ac988f'),
    'sqlwitness': (OLD, 'd4ff8d2a4d4d83c8f4dd6cd6a72f686605d2b4a7'),
    'sqlwitness_representative': (OLD, 'd4ff8d2a4d4d83c8f4dd6cd6a72f686605d2b4a7'),
}
SEEDS = tuple(range(5))
SIZES = (10, 25, 50, 100, 200, 500)
OUTPUT = NEW / 'scores-v1'
VERIFIED_FILES: dict[str, str] = {}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_batches() -> dict[str, dict]:
    test_ids = [line.strip() for line in (DATA / 'test.ids').read_text().splitlines() if line.strip()]
    assert len(test_ids) == 108 and len(set(test_ids)) == 108
    all_rows = [json.loads(line) for line in (DATA / 'leetcode_scale.jsonl').read_text().splitlines() if line.strip()]
    by_id = {row['batch_id']: row for row in all_rows}
    assert len(by_id) == len(all_rows) and set(test_ids) <= set(by_id)
    selected = {bid: by_id[bid] for bid in test_ids}
    parents = defaultdict(set)
    for bid, row in selected.items():
        meta = row['meta']
        assert meta['split'] == 'test' and meta['n'] in SIZES
        assert len(row['queries']) == meta['n'] and len({q['qid'] for q in row['queries']}) == meta['n']
        parents[meta['n']].add(meta['parent_batch_id'])
    assert set(parents) == set(SIZES)
    assert all(len(group) == 18 for group in parents.values())
    assert all(group == parents[SIZES[0]] for group in parents.values())
    return selected


def source_path(bid: str, method: str, seed: int) -> Path:
    root = SOURCES[method][0]
    original = root / method / f's{seed}' / bid
    if method in ('sqlwitness', 'sqlwitness_representative') and seed == 2:
        ids = (DATA / 'test.ids').read_text().splitlines() + (DATA / 'dev.ids').read_text().splitlines()
        if bid in ids[2::3]:
            recovered = SUPPLEMENT / method / f's{seed}' / bid
            if (recovered / 'result.json').is_file():
                assert not (original / 'result.json').exists(), 'ambiguous original and supplement'
                return recovered
    return original


def expected_paths(batches: dict[str, dict]):
    for bid in batches:
        for method in SOURCES:
            for seed in SEEDS:
                yield bid, method, seed, source_path(bid, method, seed)


def read_unit(bid: str, method: str, seed: int, path: Path, batch: dict) -> dict:
    config_bytes = (path / 'config.json').read_bytes()
    config = json.loads(config_bytes)
    result = json.loads((path / 'result.json').read_text())
    metrics = result['metrics']
    assert result['config_sha256'] == hashlib.sha256(config_bytes).hexdigest()
    assert config['source_commit'] == SOURCES[method][1]
    assert (config['batch'], config['method'], config['seed']) == (bid, method, seed)
    assert config['order'] == 'shuffle' and config['method_seed'] == 0
    assert config['budget_s'] == 600 and config['temporary_directory'] == (TMPDIR)
    assert config['files']
    for filename, digest in config['files'].items():
        if filename not in VERIFIED_FILES:
            assert sha(Path(filename)) == digest, filename
            VERIFIED_FILES[filename] = digest
        else:
            assert VERIFIED_FILES[filename] == digest, filename
    ids = [q['qid'] for q in batch['queries']]
    assert config['query_ids'] == ids
    shuffled = list(ids)
    random.Random(seed).shuffle(shuffled)
    arrivals = [event['query'] for event in result['events'] if event.get('event') == 'arrival']
    assert arrivals == shuffled[:metrics['processed_queries']]
    assert result['unprocessed_query_ids'] == shuffled[metrics['processed_queries']:]
    n = batch['meta']['n']
    processed = metrics['processed_queries']
    assert metrics['input_queries'] == n
    assert processed + metrics['unprocessed_queries'] == n
    assert metrics['invalid_admitted_certificates'] == 0
    assert sum(result['pair_counts'].values()) == processed * (processed - 1) // 2
    assert metrics['completed_within_budget'] == (processed == n and metrics['wall_s'] <= 600)
    assert metrics['censored_at_budget'] == (not metrics['completed_within_budget'])
    assert all(event['wall_s'] < 600 for event in result['events']
               if event.get('event') in ('arrival', 'pool_added', 'evidence_edge_admitted'))
    calls = metrics['pair_decision_requests']
    if method.startswith('sqlwitness'):
        assert calls == metrics['native_pair_calls']
    assert calls >= 0 and metrics['solver_calls'] >= 0
    return {
        'batch_id': bid,
        'parent_batch_id': batch['meta']['parent_batch_id'],
        'n': n,
        'method': method,
        'seed': seed,
        'input_queries': n,
        'processed_queries': processed,
        'completed_within_budget': int(metrics['completed_within_budget']),
        'censored_at_budget': int(metrics['censored_at_budget']),
        'wall_s': metrics['wall_s'],
        'cpu_s': metrics['cpu_s'],
        'pair_decision_requests': calls,
        'solver_check_attempts': metrics['solver_calls'],
        'quick_witness_calls': metrics.get('quick_witness_calls', 0),
        'pool_size': metrics['pool_size'],
        'class_count': result['class_count'],
        'separated_pairs': result['pair_counts']['separated'],
        'proved_pairs': result['pair_counts']['proved'],
        'bounded_pairs': result['pair_counts']['bounded'],
        'undecided_pairs': result['pair_counts']['undecided'],
        'invalid_admitted_certificates': metrics['invalid_admitted_certificates'],
        'config_sha256': result['config_sha256'],
        'result_sha256': sha(path / 'result.json'),
        'source_commit': config['source_commit'],
        'source_cohort': path.parents[2].name,
    }


def ratio(numerator: float, denominator: float):
    return numerator / denominator if denominator else None


def mean_sd(values):
    assert values and all(math.isfinite(v) for v in values)
    return statistics.mean(values), statistics.stdev(values)


def csv_data(rows: list[dict]) -> str:
    assert rows
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def aggregate(units: list[dict]):
    groups = defaultdict(list)
    for unit in units:
        groups[unit['method'], unit['seed'], unit['n']].append(unit)
    assert len(groups) == len(SOURCES) * len(SEEDS) * len(SIZES)
    per_seed = []
    for (method, seed, n), rows in sorted(groups.items()):
        assert len(rows) == 18 and len({row['parent_batch_id'] for row in rows}) == 18
        processed = sum(row['processed_queries'] for row in rows)
        inputs = sum(row['input_queries'] for row in rows)
        wall = sum(row['wall_s'] for row in rows)
        calls = sum(row['pair_decision_requests'] for row in rows)
        checks = sum(row['solver_check_attempts'] for row in rows)
        quick = sum(row['quick_witness_calls'] for row in rows)
        per_seed.append({
            'method': method, 'seed': seed, 'n': n, 'batches': 18,
            'input_queries': inputs, 'processed_queries': processed,
            'complete_batches': sum(row['completed_within_budget'] for row in rows),
            'censored_batches': sum(row['censored_at_budget'] for row in rows),
            'throughput_queries_s': processed / wall,
            'processed_fraction': processed / inputs,
            'pair_decisions_per_processed_query': ratio(calls, processed),
            'solver_checks_per_processed_query': ratio(checks, processed),
            'quick_calls_per_processed_query': ratio(quick, processed),
            'wall_sum_s': wall,
            'cpu_sum_s': sum(row['cpu_s'] for row in rows),
            'mean_pool_size': statistics.mean(row['pool_size'] for row in rows),
            'mean_class_count': statistics.mean(row['class_count'] for row in rows),
        })
    summary = []
    numeric = [key for key in per_seed[0] if key not in ('method', 'seed', 'n')]
    for method in SOURCES:
        for n in SIZES:
            rows = [row for row in per_seed if row['method'] == method and row['n'] == n]
            assert len(rows) == 5 and {row['seed'] for row in rows} == set(SEEDS)
            for metric in numeric:
                values = [row[metric] for row in rows]
                if any(value is None for value in values):
                    avg = sd = None
                else:
                    avg, sd = mean_sd(values)
                summary.append({'method': method, 'n': n, 'metric': metric,
                                'mean': avg, 'sample_sd': sd, 'repeats': 5})
    return per_seed, summary


def recovery_note(units: list[dict]) -> dict:
    """Keep the frozen infrastructure failure visible in published scores."""
    failed = OLD / 'sqlwitness/s2/leetcode-1132-n50'
    expected_hashes = {
        'config.json': '4336839b6d27f210fab77ffad6fef35f5dd3c11f2a9102800a615de3166106a0',
        'entry-metrics.jsonl': '12a80f29ddc94d05aeeaa630cb29fef523efddaa8587577ae764a1a60f7c7bd2',
        'service-exit.code': '4355a46b19d348dc2f57c046f8ef63d4538ebb936000f3c9ee954a27460dd865',
    }
    assert all(sha(failed / name) == digest for name, digest in expected_hashes.items())
    assert not (failed / 'result.json').exists()
    all_ids = [line.strip() for filename in ('test.ids', 'dev.ids')
               for line in (DATA / filename).read_text().splitlines() if line.strip()]
    shard_ids = all_ids[2::3]
    original_dirs = [OLD / method / 's2' / bid for bid in shard_ids
                     for method in ('seer_stream', 'seer_stream_sqlwitness',
                                    'sqlwitness', 'sqlwitness_representative')]
    assert len(original_dirs) == 152
    completed = sum((path / 'result.json').is_file() for path in original_dirs)
    failed_count = sum((path / 'service-exit.code').is_file() for path in original_dirs)
    unstarted = sum(not path.exists() for path in original_dirs)
    assert (completed, failed_count, unstarted) == (16, 1, 135)
    supplemented = [row for row in units
                    if row['source_cohort'] == SUPPLEMENT.name]
    assert all(row['seed'] == 2 and row['method'] in
               ('sqlwitness', 'sqlwitness_representative') for row in supplemented)
    return {'original_failed_units': failed_count,
            'original_unstarted_units': unstarted,
            'original_completed_units': completed,
            'supplement_units_in_this_score': len(supplemented),
            'original_failure': str(failed),
            'original_failure_sha256': expected_hashes,
            'supplement_cohort': SUPPLEMENT.name}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    batches = source_batches()
    paths = list(expected_paths(batches))
    assert len(paths) == 108 * 5 * len(SOURCES)
    # A live runner writes result.json directly. Snapshot only files whose
    # mtime predates this scan, so newly appearing files cannot change the
    # complete/missing partition or be read mid-write.
    cutoff = time.time() - 2.0
    available = [(bid, method, seed, path) for bid, method, seed, path in paths
                 if (path / 'result.json').is_file()
                 and (path / 'result.json').stat().st_mtime < cutoff]
    available_paths = {path for _, _, _, path in available}
    # Validate each immutable unit as soon as it lands, while withholding all
    # aggregate scores until the frozen matrix is complete.
    units = [read_unit(bid, method, seed, path, batches[bid])
             for bid, method, seed, path in available]
    if len(available) != len(paths):
        missing = [str(path) for _, _, _, path in paths if path not in available_paths]
        assert len(units) + len(missing) == len(paths)
        print(json.dumps({'state': 'waiting_no_scores_published', 'expected_units': len(paths),
                          'validated_units': len(units), 'missing_units': len(missing),
                          'first_missing': missing[:4]}, sort_keys=True))
        return 2
    per_seed, summary = aggregate(units)
    output = args.output
    files = {'per-unit.csv': csv_data(units), 'per-seed.csv': csv_data(per_seed),
             'summary.csv': csv_data(summary),
             'scores.json': json.dumps({'state': 'complete', 'expected_units': len(paths),
                                        'infrastructure_recovery': recovery_note(units),
                                        'source_input_sha256': sha(DATA / 'leetcode_scale.jsonl'),
                                        'test_ids_sha256': sha(DATA / 'test.ids'),
                                        'methods': {method: commit for method, (_, commit) in SOURCES.items()},
                                        'definitions': {
                                            'throughput_queries_s': 'processed arrivals divided by full observed wall, including censored batches',
                                            'pair_decisions_per_processed_query': 'representative comparisons or released SQLWitness pair calls divided by processed arrivals',
                                            'solver_checks_per_processed_query': 'observed host SMT check starts; SQLWitness native internals unavailable',
                                            'repeats': 'pool 18 held-out parent batches within each arrival seed; sample SD over seeds',
                                            'censoring': 'full input denominator retained; complete and censored batch counts shown separately',
                                        }}, sort_keys=True, indent=2) + '\n'}
    if output.exists():
        assert {path.name for path in output.iterdir()} == set(files)
        assert all((output / name).read_text() == value for name, value in files.items())
    else:
        temporary = output.with_name(output.name + '.pending')
        assert not temporary.exists()
        temporary.mkdir(parents=True)
        for name, value in files.items():
            (temporary / name).write_text(value)
        temporary.rename(output)
    print(json.dumps({'state': 'published_complete_scores', 'output': str(output),
                      'units': len(units), 'scores_sha256': sha(output / 'scores.json')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
