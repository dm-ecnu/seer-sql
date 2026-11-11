"""Score SEER on Polygon's published D-50/D-100 disambiguation tasks.

Polygon stops when one database distinguishes the candidates. SEER continues
partitioning/proving, so its task-completion time is the first certified split
event; the full partition wall clock is retained separately.
"""
from __future__ import annotations

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse
import csv
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
from statistics import median

ROOT = Path((SEER_ROOT))
DATA = ROOT / 'datasets/batches'
RUN = ROOT / 'repro/polygon-original-d50-d100-v1/seer-complete-v2'
OFFICIAL = ROOT / 'repro/polygon-original-d50-d100-v1'
FROZEN_COMMIT = 'b0c45c8'
EXPECTED = {50: 4245, 100: 2475}


def read_json(path):
    return json.loads(path.read_text())


@lru_cache(maxsize=None)
def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def score_one(batch, status, output):
    """Return one fully checked task record from its raw runner receipt."""
    size = len(batch['queries'])
    if status['size'] != size or status['batch'] != batch['batch_id']:
        raise ValueError('status does not identify this original task')
    if status['exit_code'] != 0 or status['status'] != 'complete':
        raise ValueError('failed task cannot enter the main aggregate')
    config = read_json(output / 'config.json')
    result = read_json(output / 'result.json')
    if (not config['source_commit'].startswith(FROZEN_COMMIT) or
            config['method'] != 'seer_stream' or config['ablation'] != 'none' or
            config['seed'] != 0 or config['stream_schedule'] != 'tail_proof' or
            config['budget_s'] != 60 or config['order'] != 'source' or
            config['query_ids'] != [q['qid'] for q in batch['queries']] or
            config['batch'] != batch['batch_id']):
        raise ValueError('frozen source/input/budget differs from the original task')
    source = DATA / f'cubes_d{size}.jsonl'
    schema = DATA / batch['schema_path']
    if (config['files'].get(str(source)) != sha(str(source)) or
            config['files'].get(str(schema)) != sha(str(schema))):
        raise ValueError('original task input/schema hash changed')
    metrics = result['metrics']
    if (metrics['input_queries'] != size or metrics['processed_queries'] != size or
            metrics['unprocessed_queries'] != 0 or
            metrics['invalid_admitted_certificates'] != 0 or
            result['contract'] != 'canonical-6sig-list-bag-errors-v1'):
        raise ValueError('incomplete or uncertified partition')
    if abs(metrics['wall_s'] - status['method_wall_s']) > 1e-6:
        raise ValueError('status/receipt wall clock differs')
    solved = result['pair_counts']['separated'] > 0
    if solved != status['solved']:
        raise ValueError('status/receipt solved classification differs')
    events = result['events']
    timed_events = [e for e in events if 'wall_s' in e]
    if any(a['wall_s'] > b['wall_s'] for a, b in zip(timed_events, timed_events[1:])):
        raise ValueError('event time order changed')
    split_events = [e for e in timed_events if e.get('separated_pairs', 0) > 0]
    if solved != bool(split_events):
        raise ValueError('solved task lacks its first certified split event')
    first_s = split_events[0]['wall_s'] if split_events else None
    if first_s is not None and not (0 <= first_s <= metrics['wall_s'] <= 60.5):
        raise ValueError('invalid split/wall timing')
    if solved and not any(w['active'] for w in result['witnesses']):
        raise ValueError('solved task has no active certified database')
    if solved and any(not w['active'] for w in result['witnesses']):
        raise ValueError('early split may rely on a later disabled database; replay required')
    return {'batch': batch['batch_id'], 'size': size, 'solved': solved,
            'time_to_solved_s': first_s, 'full_partition_wall_s': metrics['wall_s'],
            'solver_calls': metrics['solver_calls'],
            'invalid_admitted_certificates': 0}


def score_official(size, batches, path):
    """Read the unchanged author's CSV once the complete panel is present."""
    if not path.exists():
        return None
    with path.open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != EXPECTED[size]:
        return None
    by_original_id = {str(b['meta']['polygon_id']): b for b in batches.values()}
    if len(by_original_id) != EXPECTED[size]:
        raise ValueError('original Polygon task ID is missing or duplicated')
    seen = set()
    solved_times = []
    status_counts = {'True': 0, 'False': 0, 'ERR': 0}
    for row in rows:
        original_id = row['ID']
        if original_id in seen or original_id not in by_original_id:
            raise ValueError('duplicate/unknown task in official Polygon CSV')
        seen.add(original_id)
        if row['Benchmark'] != by_original_id[original_id]['meta']['benchmark']:
            raise ValueError('official Polygon task benchmark differs')
        verdict = row['?Solved']
        if verdict not in status_counts:
            raise ValueError('unrecognized official Polygon verdict')
        status_counts[verdict] += 1
        if verdict == 'True':
            time_s = float(row['Time'])
            if not math.isfinite(time_s) or time_s < 0:
                raise ValueError('invalid official Polygon solved time')
            solved_times.append(time_s)
    return {'tasks': len(rows), 'solved': status_counts['True'],
            'unsolved': status_counts['False'], 'errors': status_counts['ERR'],
            'solved_rate': len(solved_times) / len(rows),
            'median_solved_time_s': median(solved_times) if solved_times else None,
            'mean_solved_time_s': sum(solved_times) / len(solved_times) if solved_times else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', type=Path, default=RUN / 'status.jsonl')
    parser.add_argument('--run-dir', type=Path, default=RUN)
    parser.add_argument('--official-root', type=Path, default=OFFICIAL)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    by_size = {}
    for size, expected in EXPECTED.items():
        path = DATA / f'cubes_d{size}.jsonl'
        batches = [json.loads(line) for line in path.open()]
        if len(batches) != expected:
            raise ValueError('published D denominator changed')
        ids = [b['batch_id'] for b in batches]
        if len(ids) != len(set(ids)) or any(len(b['queries']) != size for b in batches):
            raise ValueError('duplicate or malformed original D task')
        by_size[size] = {b['batch_id']: b for b in batches}
    statuses = [json.loads(line) for line in args.status.open()]
    seen = set()
    scored = {50: [], 100: []}
    for status in statuses:
        size, batch_id = status['size'], status['batch']
        key = size, batch_id
        if key in seen:
            raise ValueError('duplicate task status; resolve failed/retry provenance first')
        seen.add(key)
        if size not in by_size or batch_id not in by_size[size]:
            raise ValueError('status is outside the published D tasks')
        output = args.run_dir / f'd{size}' / batch_id
        scored[size].append(score_one(by_size[size][batch_id], status, output))
    official = {size: score_official(
        size, by_size[size], args.official_root / f'd{size}' /
        f'experiment_result_cubes_disambiguation_{size}_False.csv')
        for size in EXPECTED}
    complete = (all(len(scored[s]) == n for s, n in EXPECTED.items()) and
                all(official[s] is not None for s in EXPECTED))
    if not complete:
        print(json.dumps({'state': 'waiting_for_full_denominator',
                          'verified_tasks': {str(s): len(scored[s]) for s in EXPECTED},
                          'expected_tasks': EXPECTED,
                          'official_complete': {str(s): official[s] is not None for s in EXPECTED}},
                         sort_keys=True))
        return
    summary = {}
    for size, rows in scored.items():
        solved = [r for r in rows if r['solved']]
        summary[str(size)] = {
            'tasks': len(rows), 'solved': len(solved),
            'solved_rate': len(solved) / len(rows),
            'median_time_to_solved_s': median(r['time_to_solved_s'] for r in solved)
            if solved else None,
            'mean_time_to_solved_s': sum(r['time_to_solved_s'] for r in solved) / len(solved)
            if solved else None,
            'mean_full_partition_wall_s': sum(r['full_partition_wall_s'] for r in rows) / len(rows),
            'invalid_admitted_certificates': 0,
        }
    result = {'source_commit': FROZEN_COMMIT, 'seer': summary,
              'official_polygon': {str(s): official[s] for s in EXPECTED},
              'tasks': scored}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'source_commit': FROZEN_COMMIT, 'seer': summary,
                      'official_polygon': result['official_polygon']}, sort_keys=True))


if __name__ == '__main__':
    main()
