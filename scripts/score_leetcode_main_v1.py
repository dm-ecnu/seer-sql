"""Seven-method LeetCode-500 main-table score, withheld until every frozen unit exists.

SQLDriller's released checker has no declared resources for these 19 MySQL
schemas; that coverage is reported separately, never imputed as a measured run.
"""
from __future__ import annotations

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import csv
import hashlib
import io
import json
import math
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import score_leetcode_stream_v10 as streamscore

BASE = Path((SEER_ROOT))
RESULTS = BASE / 'repro/stream-partition-v1'
PAIR = RESULTS / 'pairseer-leetcode-main-v1'
METHODS = ('seer_stream', 'seer_stream_sqlwitness', 'sqlwitness',
           'sqlwitness_representative', 'parseval', 'xdatan', 'pair_seer')
SEEDS = tuple(range(5))
N = 500
PAIRS = N * (N-1) // 2
OUTPUT = RESULTS / 'leetcode-main-score-v1'
CHECKER = Path(__file__).with_name('check_pairseer_leetcode_unit_v1.py')
streamscore.SOURCES.update({
    'parseval': (RESULTS / 'leetcode-opponents-matrix-v7',
                 'd4ff8d2a4d4d83c8f4dd6cd6a72f686605d2b4a7'),
    'xdatan': (RESULTS / 'leetcode-opponents-matrix-v7',
               'd4ff8d2a4d4d83c8f4dd6cd6a72f686605d2b4a7'),
})


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pair_position(i: int, j: int) -> int:
    assert 0 <= i < j < N
    return i * (2*N-i-1) // 2 + j-i-1


def stream_mask(result: dict, ids: list[str]) -> int:
    labels = {}
    for group, members in enumerate(result['classes']):
        for qid in members:
            assert qid not in labels
            labels[qid] = group
    assert set(labels) <= set(ids)
    assert len(labels) == result['metrics']['processed_queries']
    assert len(labels) == sum(len(group) for group in result['classes'])
    present = [labels.get(qid) for qid in ids]
    bits = bytearray(PAIRS)
    for i in range(N-1):
        left = present[i]
        if left is None:
            continue
        start = pair_position(i, i+1)
        for j in range(i+1, N):
            right = present[j]
            if right is not None and left != right:
                bits[start+j-i-1] = 1
    mask = int.from_bytes(bits, 'little')
    assert mask.bit_count() == result['pair_counts']['separated']
    assert len(labels) * (len(labels)-1) // 2 == sum(result['pair_counts'].values())
    return mask


def pairseer_mask(result: dict, ids: list[str]) -> int:
    indexes = {qid: i for i, qid in enumerate(ids)}
    assert len(indexes) == N and len(result['pairs']) == PAIRS
    bits = bytearray(PAIRS)
    seen = set()
    for pair in result['pairs']:
        i, j = sorted((indexes[pair['a']], indexes[pair['b']]))
        position = pair_position(i, j)
        assert position not in seen
        seen.add(position)
        if pair['verdict'] == 'split':
            assert pair['accepted_s'] < 600
            bits[position] = 1
        else:
            assert pair['verdict'] == 'undecided'
    mask = int.from_bytes(bits, 'little')
    assert len(seen) == PAIRS and mask.bit_count() == result['n_genuine_pairs']
    return mask


def path_for(method: str, seed: int, bid: str) -> Path:
    if method == 'pair_seer':
        return PAIR / 'units' / f's{seed}' / bid
    return streamscore.source_path(bid, method, seed)


def read_unit(method: str, seed: int, batch: dict, path: Path, ids: list[str]):
    bid = batch['batch_id']
    if method == 'pair_seer':
        subprocess.run([sys.executable, str(CHECKER), str(path)], check=True,
                       capture_output=True, text=True)
        result = json.loads((path / 'raw.jsonl').read_text())
        config = json.loads((path / 'config.json').read_text())
        assert config['batch'] == bid and config['arrival_seed'] == seed
        mask = pairseer_mask(result, ids)
        counts = result['call_metrics']['counts']
        row = {'processed_queries': None, 'input_queries': N, 'separated_pairs': mask.bit_count(),
               'proved_pairs': None, 'bounded_pairs': None,
               'undecided_pairs': PAIRS-mask.bit_count(), 'unprocessed_pairs': 0,
               'unsupported_native_calls': None, 'invalid_admitted_certificates': 0,
               'class_count': None, 'class_size_histogram': None,
               'wall_s': result['wall_secs'], 'host_cpu_s': result['cpu_secs'],
               'service_peak_bytes': result['service_peak_memory_bytes'],
               'solver_check_attempts': counts.get('python_solver_check_attempt', 0),
               'native_pair_calls': None, 'quick_witness_calls': None,
               'accepted_databases': result['n_dbs'],
               'last_split_s': result['last_accept_s'],
               'completed_within_budget': int(result['source_status'] == 'completed'
                                               and result['wall_secs'] <= 600),
               'censored_at_budget': int(result['source_status'] == 'deadline'),
               'source_status': result['source_status'],
               'raw_sha256': sha(path / 'raw.jsonl')}
    else:
        checked = streamscore.read_unit(bid, method, seed, path, batch)
        result = json.loads((path / 'result.json').read_text())
        mask = stream_mask(result, ids)
        metrics = result['metrics']
        processed_pairs = metrics['processed_queries'] * (metrics['processed_queries']-1) // 2
        row = {'processed_queries': metrics['processed_queries'], 'input_queries': N,
               'separated_pairs': mask.bit_count(),
               'proved_pairs': result['pair_counts']['proved'],
               'bounded_pairs': result['pair_counts']['bounded'],
               'undecided_pairs': result['pair_counts']['undecided'],
               'unprocessed_pairs': PAIRS-processed_pairs,
               'unsupported_native_calls': sum(call['native']['verdict'] == 'unsupported'
                                               for call in result.get('calls', [])),
               'invalid_admitted_certificates': metrics['invalid_admitted_certificates'],
               'class_count': result['class_count'],
               'class_size_histogram': json.dumps(result['class_size_histogram'], sort_keys=True),
               'wall_s': metrics['wall_s'], 'host_cpu_s': metrics['cpu_s'],
               'service_peak_bytes': int(result['cgroup_resources']['memory_peak_bytes']),
               'solver_check_attempts': metrics['solver_calls'],
               'native_pair_calls': metrics.get('native_pair_calls'),
               'quick_witness_calls': metrics['quick_witness_calls'],
               'accepted_databases': metrics['pool_size'],
               'last_split_s': None,
               'completed_within_budget': int(metrics['completed_within_budget']),
               'censored_at_budget': int(metrics['censored_at_budget']),
               'source_status': 'completed' if metrics['completed_within_budget'] else 'deadline',
               'raw_sha256': checked['result_sha256']}
        assert sum(result['pair_counts'].values()) + row['unprocessed_pairs'] == PAIRS
    row['source_cohort'] = path.parents[2].name
    assert row['invalid_admitted_certificates'] == 0
    return mask, {'batch_id': bid, 'seed': seed, 'method': method, 'possible_pairs': PAIRS, **row}


def csv_text(rows):
    assert rows
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def main():
    batches = {bid: batch for bid, batch in streamscore.source_batches().items()
               if batch['meta']['n'] == N}
    assert len(batches) == 18
    expected = [(bid, method, seed, path_for(method, seed, bid))
                for bid in batches for method in METHODS for seed in SEEDS]
    assert len(expected) == 630
    cutoff = time.time() - 2.0
    available = [(bid, method, seed, path) for bid, method, seed, path in expected
                 if (path / ('raw.jsonl' if method == 'pair_seer' else 'result.json')).is_file()
                 and (path / ('raw.jsonl' if method == 'pair_seer' else 'result.json')).stat().st_size > 0
                 and (path / ('raw.jsonl' if method == 'pair_seer' else 'result.json')).stat().st_mtime < cutoff]
    if len(available) != len(expected):
        # Validate landed units, but never publish a partial common denominator.
        for bid, method, seed, path in available:
            batch = batches[bid]
            read_unit(method, seed, batch, path, [q['qid'] for q in batch['queries']])
        present = {path for _, _, _, path in available}
        missing = [str(path) for _, _, _, path in expected if path not in present]
        print(json.dumps({'state': 'waiting_no_scores_published', 'expected_units': 630,
                          'validated_units': len(available), 'missing_units': len(missing),
                          'first_missing': missing[:4]}, sort_keys=True))
        return 2
    units = []
    representative_misses = []
    known_by_batch = {}
    for bid, batch in batches.items():
        ids = [q['qid'] for q in batch['queries']]
        masks = {}
        rows = {}
        for method in METHODS:
            for seed in SEEDS:
                masks[method, seed], rows[method, seed] = read_unit(
                    method, seed, batch, path_for(method, seed, bid), ids)
        known_mask = 0
        for mask in masks.values():
            known_mask |= mask
        known = known_mask.bit_count()
        assert known > 0
        known_by_batch[bid] = known
        for method in METHODS:
            for seed in SEEDS:
                mask = masks[method, seed]
                assert (mask & known_mask) == mask
                row = rows[method, seed]
                row['known_neq_pairs'] = known
                row['known_neq_recall'] = mask.bit_count() / known
                units.append(row)
        for seed in SEEDS:
            full = masks['sqlwitness', seed]
            representative = masks['sqlwitness_representative', seed]
            representative_misses.append({'batch_id': bid, 'seed': seed,
                                          'full_shell_separated': full.bit_count(),
                                          'representative_shell_separated': representative.bit_count(),
                                          'full_shell_only_pairs': (full & ~representative).bit_count()})
    assert len(units) == 630 and len(representative_misses) == 90
    per_seed = []
    for method in METHODS:
        for seed in SEEDS:
            rows = [row for row in units if row['method'] == method and row['seed'] == seed]
            assert len(rows) == 18
            den = sum(row['known_neq_pairs'] for row in rows)
            split = sum(row['separated_pairs'] for row in rows)
            wall = sum(row['wall_s'] for row in rows)
            per_seed.append({'method': method, 'seed': seed, 'batches': 18,
                             'known_neq_pairs': den, 'separated_pairs': split,
                             'known_neq_recall': split / den,
                             'processed_queries': (sum(row['processed_queries'] for row in rows)
                                                   if all(row['processed_queries'] is not None for row in rows) else None),
                             'input_queries': 18*N, 'wall_sum_s': wall,
                             'processed_queries_per_s': (sum(row['processed_queries'] for row in rows)/wall
                                                         if all(row['processed_queries'] is not None for row in rows) else None),
                             'offered_queries_per_s': 18*N/wall,
                             'host_cpu_sum_s': sum(row['host_cpu_s'] for row in rows),
                             'mean_service_peak_mib': statistics.mean(row['service_peak_bytes'] for row in rows)/2**20,
                             'solver_check_attempts': sum(row['solver_check_attempts'] for row in rows),
                             'native_pair_calls': (sum(row['native_pair_calls'] for row in rows)
                                                   if all(row['native_pair_calls'] is not None for row in rows) else None),
                             'quick_witness_calls': (sum(row['quick_witness_calls'] for row in rows)
                                                     if all(row['quick_witness_calls'] is not None for row in rows) else None),
                             'separated_fraction': sum(row['separated_pairs'] for row in rows)/(18*PAIRS),
                             'proved_fraction': (sum(row['proved_pairs'] for row in rows)/(18*PAIRS)
                                                 if all(row['proved_pairs'] is not None for row in rows) else None),
                             'bounded_fraction': (sum(row['bounded_pairs'] for row in rows)/(18*PAIRS)
                                                  if all(row['bounded_pairs'] is not None for row in rows) else None),
                             'undecided_fraction': sum(row['undecided_pairs'] for row in rows)/(18*PAIRS),
                             'unprocessed_fraction': sum(row['unprocessed_pairs'] for row in rows)/(18*PAIRS),
                             'unsupported_native_calls': (sum(row['unsupported_native_calls'] for row in rows)
                                                          if all(row['unsupported_native_calls'] is not None for row in rows) else None),
                             'invalid_admitted_certificates': sum(row['invalid_admitted_certificates'] for row in rows),
                             'completed_batches': sum(row['completed_within_budget'] for row in rows),
                             'censored_batches': sum(row['censored_at_budget'] for row in rows),
                             'source_error_batches': sum(row['source_status'] not in ('completed', 'deadline') for row in rows)})
    summary = []
    for method in METHODS:
        rows = [row for row in per_seed if row['method'] == method]
        assert {row['seed'] for row in rows} == set(SEEDS)
        for metric in rows[0]:
            if metric in ('method', 'seed'):
                continue
            values = [row[metric] for row in rows]
            summary.append({'method': method, 'metric': metric,
                            'mean': statistics.mean(values) if all(v is not None and math.isfinite(v) for v in values) else None,
                            'sample_sd': statistics.stdev(values) if all(v is not None and math.isfinite(v) for v in values) else None})
    files = {'per-unit.csv': csv_text(units), 'per-seed.csv': csv_text(per_seed),
             'summary.csv': csv_text(summary),
             'representative-miss.csv': csv_text(representative_misses),
             'scores.json': json.dumps({'state': 'complete', 'expected_units': 630,
                                        'infrastructure_recovery': streamscore.recovery_note(units),
                                        'methods': list(METHODS), 'seeds': list(SEEDS),
                                        'batches': list(batches), 'known_neq_by_batch': known_by_batch,
                                        'scorer_sha256': sha(Path(__file__)),
                                        'source_input_sha256': sha(streamscore.DATA / 'leetcode_scale.jsonl'),
                                        'definitions': {'known_neq': 'per held-out batch union of certified separated pairs across seven measured methods and five arrival seeds',
                                                        'sql_driller': 'no declared resource for any LeetCode schema; absent from measured denominator, report unsupported separately',
                                                        'unprocessed_pairs': 'full 500-query pair denominator minus pairs among admitted arrivals; censored units retained',
                                                        'pairseer_cpu': 'host cgroup only; external MySQL CPU unavailable',
                                                        'solver_primitives': 'SMT starts and native pair calls stored separately, never summed'},
                                        }, sort_keys=True, indent=2) + '\n'}
    if OUTPUT.exists():
        assert {path.name for path in OUTPUT.iterdir()} == set(files)
        assert all((OUTPUT / name).read_text() == content for name, content in files.items())
    else:
        pending = OUTPUT.with_name(OUTPUT.name + '.pending')
        assert not pending.exists()
        pending.mkdir()
        for name, content in files.items():
            (pending / name).write_text(content)
        pending.rename(OUTPUT)
    print(json.dumps({'state': 'published_complete_scores', 'units': len(units),
                      'output': str(OUTPUT), 'scores_sha256': sha(OUTPUT / 'scores.json')}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
