"""Score the 18×5 held-out PairSeer B600 cohort after every unit arrives."""
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
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path((SEER_ROOT))
ROOT = BASE / 'repro/stream-partition-v1/pairseer-leetcode-main-v1'
CHECKER = Path(__file__).with_name('check_pairseer_leetcode_unit_v1.py')
SEEDS = tuple(range(5))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_text(rows):
    assert rows
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def read_unit(path: Path) -> dict:
    checked = subprocess.run([sys.executable, str(CHECKER), str(path)],
                             check=True, capture_output=True, text=True)
    proof = json.loads(checked.stdout)
    row = json.loads((path / 'raw.jsonl').read_text())
    config = json.loads((path / 'config.json').read_text())
    counts = row['call_metrics']['counts']
    split = row['n_genuine_pairs']
    assert split == proof['genuine_pairs']
    assert row['acceptance_window_s'] <= 600
    assert row['wall_secs'] >= row['acceptance_window_s']
    assert row['cpu_host_complete'] is True
    assert row['service_peak_memory_bytes'] is not None
    return {'batch_id': config['batch'], 'seed': config['arrival_seed'],
            'source_status': row['source_status'], 'source_error': row['worker_error'],
            'input_queries': 500, 'possible_pairs': 500 * 499 // 2,
            'separated_pairs': split, 'separated_fraction': split / (500 * 499 // 2),
            'accepted_databases': row['n_dbs'], 'last_accept_s': row['last_accept_s'],
            'acceptance_window_s': row['acceptance_window_s'], 'wall_s': row['wall_secs'],
            'host_cpu_s': row['cpu_secs'], 'service_peak_memory_bytes': row['service_peak_memory_bytes'],
            'solver_check_attempts': counts.get('python_solver_check_attempt', 0),
            'source_query_execute_attempts': counts.get('source_query_execute_attempt', 0),
            'common_query_execute_attempts': counts.get('common_query_execute_attempt', 0),
            'accepted_receipts': len(row['accepted_receipts']),
            'raw_sha256': proof['raw_sha256'], 'config_sha256': sha(path / 'config.json')}


def main():
    ids = [line.strip() for line in (ROOT / 'input/test-n500.ids').read_text().splitlines() if line.strip()]
    assert len(ids) == 18 and len(set(ids)) == 18
    manifest = json.loads((ROOT / 'input/arrival-inputs.json').read_text())
    assert manifest['batches'] == 18 and manifest['seeds'] == list(SEEDS)
    expected = [(seed, bid, ROOT / 'units' / f's{seed}' / bid) for seed in SEEDS for bid in ids]
    cutoff = time.time() - 2.0
    available = [(seed, bid, path) for seed, bid, path in expected
                 if (path / 'raw.jsonl').is_file() and (path / 'raw.jsonl').stat().st_size > 0
                 and (path / 'raw.jsonl').stat().st_mtime < cutoff]
    units = [read_unit(path) for _, _, path in available]
    if len(available) != len(expected):
        print(json.dumps({'state': 'waiting_no_scores_published', 'expected_units': len(expected),
                          'validated_units': len(units), 'missing_units': len(expected)-len(units),
                          'first_missing': [str(path) for _, _, path in expected
                                            if not any(path == found for _, _, found in available)][:4]},
                         sort_keys=True))
        return 2
    assert {(row['seed'], row['batch_id']) for row in units} == {(s, b) for s in SEEDS for b in ids}
    per_seed = []
    for seed in SEEDS:
        rows = [row for row in units if row['seed'] == seed]
        assert len(rows) == 18
        statuses = Counter(row['source_status'] for row in rows)
        last = [row['last_accept_s'] for row in rows if row['last_accept_s'] is not None]
        wall = sum(row['wall_s'] for row in rows)
        per_seed.append({'seed': seed, 'batches': 18, 'input_queries': 9000,
                         'possible_pairs': sum(row['possible_pairs'] for row in rows),
                         'separated_pairs': sum(row['separated_pairs'] for row in rows),
                         'separated_fraction': statistics.mean(row['separated_fraction'] for row in rows),
                         'completed_batches': statuses['completed'], 'deadline_batches': statuses['deadline'],
                         'source_error_batches': sum(value for key, value in statuses.items()
                                                     if key not in ('completed', 'deadline')),
                         'wall_sum_s': wall, 'offered_queries_per_s': 9000 / wall,
                         'host_cpu_sum_s': sum(row['host_cpu_s'] for row in rows),
                         'mean_service_peak_mib': statistics.mean(row['service_peak_memory_bytes'] for row in rows) / 2**20,
                         'max_service_peak_mib': max(row['service_peak_memory_bytes'] for row in rows) / 2**20,
                         'mean_last_accept_s_on_nonempty': statistics.mean(last) if last else None,
                         'no_split_batches': 18-len(last),
                         'solver_check_attempts': sum(row['solver_check_attempts'] for row in rows),
                         'source_query_execute_attempts': sum(row['source_query_execute_attempts'] for row in rows),
                         'common_query_execute_attempts': sum(row['common_query_execute_attempts'] for row in rows),
                         'accepted_databases': sum(row['accepted_databases'] for row in rows)})
    summary = []
    for field in per_seed[0]:
        if field == 'seed':
            continue
        values = [row[field] for row in per_seed]
        summary.append({'metric': field,
                        'mean': statistics.mean(values) if all(value is not None and math.isfinite(value) for value in values) else None,
                        'sample_sd': statistics.stdev(values) if all(value is not None and math.isfinite(value) for value in values) else None})
    output = ROOT / 'scores-v1'
    files = {'per-unit.csv': csv_text(units), 'per-seed.csv': csv_text(per_seed),
             'summary.csv': csv_text(summary),
             'scores.json': json.dumps({'state': 'complete', 'expected_units': 90,
                                        'scorer_sha256': sha(Path(__file__)),
                                        'source_input_sha256': manifest['source_sha256'],
                                        'test_ids_sha256': manifest['ids_sha256'],
                                        'pairseer_method_commit': 'd4ff8d2a4d4d83c8f4dd6cd6a72f686605d2b4a7',
                                        'auxiliary_commit': (ROOT / 'launcher-commit.txt').read_text().strip(),
                                        'definitions': {'repeats': 'same 18 held-out n500 batches per arrival seed',
                                                        'offered_queries_per_s': '9000 input queries divided by complete observed host wall; source deadlines retained',
                                                        'host_cpu_s': 'exclusive host cgroup; external MySQL CPU unavailable',
                                                        'last_accept_s': 'last parent-received valid split certificate within B600; no-split batches tracked separately'}},
                                       sort_keys=True, indent=2) + '\n'}
    if output.exists():
        assert {p.name for p in output.iterdir()} == set(files)
        assert all((output / name).read_text() == content for name, content in files.items())
    else:
        pending = output.with_name(output.name + '.pending')
        assert not pending.exists()
        pending.mkdir()
        for name, content in files.items():
            (pending / name).write_text(content)
        pending.rename(output)
    print(json.dumps({'state': 'published_complete_scores', 'units': len(units),
                      'scores_sha256': sha(output / 'scores.json')}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
