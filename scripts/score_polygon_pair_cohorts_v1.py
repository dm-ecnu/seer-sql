"""Score complete five-seed canonical Polygon pair cohorts on the three main loads."""

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
from pathlib import Path


BASE = Path((SEER_ROOT))
INPUT = BASE / 'datasets/batches'
RESULTS = BASE / 'repro/stream-partition-v1'
COMMIT = '113c3920997e76d189e9dadfccc78871e97188fa'
LOADS = {
    'leetcode': ('leetcode-proof-canonical-v6',
                 INPUT / 'leetcode-scale-v1/leetcode_scale.jsonl',
                 (INPUT / 'leetcode-scale-v1/test.ids',), 108),
    'bird': ('bird-proof-canonical-v5',
             INPUT / 'public-batch-v1/bird_public.jsonl',
             (INPUT / 'public-batch-v1/test.ids', INPUT / 'public-batch-v1/dev.ids'), 1534),
    'cubes': ('cubes-proof-canonical-v3-isolated',
              INPUT / 'cubes-full-v1/cubes_d100.jsonl',
              (INPUT / 'cubes-full-v1/cubes_d100.test.ids',
               INPUT / 'cubes-full-v1/cubes_d100.dev.ids'), 2475),
}
SEEDS = tuple(range(5))
STAGES = ('encoding_s', 'pool_execution_s', 'refutation_search_s',
          'bounded_proof_s', 'replay_s')
CONTENDED_CORES = set(range(169, 186)) | set(range(288, 292))
BIRD_PROOF_SUPPLEMENT = RESULTS / 'bird-proof-canonical-v5-isolated-supp-v1'


def bird_isolated_shard(seed: int, shard: int) -> bool:
    return (seed == 1 and shard in (2, 3, 4, 5)) or seed == 2 or (seed == 3 and shard == 0)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_text(rows: list[dict]) -> str:
    assert rows
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def source(load: str):
    cohort, data, id_files, expected_batches = LOADS[load]
    ids = [line.strip() for path in id_files for line in path.read_text().splitlines()
           if line.strip()]
    assert len(ids) == expected_batches and len(set(ids)) == len(ids)
    records = [json.loads(line) for line in data.read_text().splitlines() if line.strip()]
    by_id = {record['batch_id']: record for record in records}
    assert len(by_id) == len(records) and set(ids) <= set(by_id)
    return cohort, data, ids, by_id


def read_unit(path: Path, bid: str, seed: int, batch: dict) -> dict:
    assert not (path / 'service-exit.code').exists()
    config_bytes = (path / 'config.json').read_bytes()
    config = json.loads(config_bytes)
    result = json.loads((path / 'result.json').read_text())
    m = result['metrics']
    ids = [q['qid'] for q in batch['queries']]
    assert config['source_commit'] == COMMIT
    assert (config['batch'], config['method'], config['seed']) == \
        (bid, 'polygon_canonical_pair', seed)
    assert config['order'] == 'shuffle' and config['method_seed'] == 0
    assert config['budget_s'] == 600
    if ('cubes-proof-canonical-v3-isolated' in path.parts or
            'bird-proof-canonical-v5' in path.parts or
            'bird-proof-canonical-v5-isolated-supp-v1' in path.parts):
        assert len(config['affinity']) == 1
        assert config['affinity'][0] not in CONTENDED_CORES
    assert config['temporary_directory'] == (TMPDIR)
    assert config['query_ids'] == ids
    assert result['config_sha256'] == hashlib.sha256(config_bytes).hexdigest()
    shuffled = list(ids)
    random.Random(seed).shuffle(shuffled)
    arrivals = [event['query'] for event in result['events']
                if event.get('event') == 'arrival']
    processed = m['processed_queries']
    assert arrivals == shuffled[:processed]
    assert result['unprocessed_query_ids'] == shuffled[processed:]
    assert processed + m['unprocessed_queries'] == m['input_queries'] == len(ids)
    pairs = processed * (processed - 1) // 2
    assert sum(result['pair_counts'].values()) == pairs
    assert m['full_pair_targets'] == pairs
    assert len(result['pair_attempts']) == m['pair_decision_requests'] <= pairs
    assert m['invalid_admitted_certificates'] == 0
    assert m['censored_at_budget'] == (not m['completed_within_budget'])
    assert all(event['wall_s'] < 600 for event in result['events']
               if event.get('event') in ('arrival', 'pool_added', 'evidence_edge_admitted'))
    peak = result['cgroup_resources'].get('memory_peak_bytes')
    assert peak is not None and int(peak) > 0
    return {
        'batch_id': bid, 'seed': seed, 'input_queries': len(ids),
        'processed_queries': processed, 'unprocessed_queries': m['unprocessed_queries'],
        'possible_pairs': len(ids) * (len(ids) - 1) // 2,
        'processed_pairs': pairs,
        'separated_pairs': result['pair_counts']['separated'],
        'proved_pairs': result['pair_counts']['proved'],
        'bounded_pairs': result['pair_counts']['bounded'],
        'undecided_pairs': result['pair_counts']['undecided'],
        'completed_within_budget': m['completed_within_budget'],
        'censored_at_budget': m['censored_at_budget'],
        'wall_s': m['wall_s'], 'host_cpu_s': m['cpu_s'],
        'service_peak_bytes': int(peak),
        'solver_check_attempts': m['solver_calls'],
        'pair_decision_requests': m['pair_decision_requests'],
        'accepted_databases': m['pool_size'],
        **{stage: m[stage] for stage in STAGES},
        'config_sha256': sha(path / 'config.json'),
        'result_sha256': sha(path / 'result.json'),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--load', choices=tuple(LOADS), required=True)
    args = parser.parse_args()
    cohort, data, ids, by_id = source(args.load)
    root = RESULTS / cohort
    expected = [(seed, bid,
                 (BIRD_PROOF_SUPPLEMENT if args.load == 'bird' and
                  bird_isolated_shard(seed, index % 6) else root) /
                 'polygon_canonical_pair' / f's{seed}' / bid)
                for seed in SEEDS for index, bid in enumerate(ids)]
    cutoff = time.time() - 2
    available = [(seed, bid, path) for seed, bid, path in expected
                 if (path / 'result.json').is_file()
                 and (path / 'result.json').stat().st_mtime < cutoff]
    rows = [read_unit(path, bid, seed, by_id[bid]) for seed, bid, path in available]
    if len(rows) != len(expected):
        found = {path for _, _, path in available}
        print(json.dumps({'state': 'waiting_no_scores_published',
                          'expected_units': len(expected), 'validated_units': len(rows),
                          'missing_units': len(expected) - len(rows),
                          'first_missing': [str(path) for _, _, path in expected
                                            if path not in found][:4]}, sort_keys=True))
        return 2
    per_seed = []
    for seed in SEEDS:
        group = [row for row in rows if row['seed'] == seed]
        assert len(group) == len(ids)
        wall = sum(row['wall_s'] for row in group)
        processed = sum(row['processed_queries'] for row in group)
        offered = sum(row['input_queries'] for row in group)
        per_seed.append({
            'seed': seed, 'batches': len(ids), 'input_queries': offered,
            'processed_queries': processed,
            'offered_queries_per_s': offered / wall,
            'processed_queries_per_s': processed / wall,
            'completed_batches': sum(row['completed_within_budget'] for row in group),
            'censored_batches': sum(row['censored_at_budget'] for row in group),
            'separated_pairs': sum(row['separated_pairs'] for row in group),
            'proved_pairs': sum(row['proved_pairs'] for row in group),
            'bounded_pairs': sum(row['bounded_pairs'] for row in group),
            'undecided_pairs': sum(row['undecided_pairs'] for row in group),
            'pair_decisions_per_processed_query':
                sum(row['pair_decision_requests'] for row in group) / processed if processed else None,
            'solver_checks_per_processed_query':
                sum(row['solver_check_attempts'] for row in group) / processed if processed else None,
            'wall_sum_s': wall,
            'host_cpu_sum_s': sum(row['host_cpu_s'] for row in group),
            'mean_service_peak_mib': statistics.mean(row['service_peak_bytes'] for row in group) / 2**20,
            **{stage: sum(row[stage] for row in group) for stage in STAGES},
        })
    summary = []
    for field in per_seed[0]:
        if field == 'seed':
            continue
        values = [row[field] for row in per_seed]
        summary.append({'metric': field,
                        'mean': statistics.mean(values) if all(v is not None and math.isfinite(v) for v in values) else None,
                        'sample_sd': statistics.stdev(values) if all(v is not None and math.isfinite(v) for v in values) else None})
    output = root / 'scores-v1'
    files = {
        'per-unit.csv': csv_text(rows), 'per-seed.csv': csv_text(per_seed),
        'summary.csv': csv_text(summary),
        'scores.json': json.dumps({
            'state': 'complete', 'expected_units': len(expected),
            'source_commit': COMMIT, 'source_input_sha256': sha(data),
            'excluded_original_cohort': (
                {'path': str(RESULTS / 'cubes-proof-canonical-v2'),
                 'reason': 'all five first units overlapped concurrent Cubes opponents on CPUs 180, 182, 184, 186, 188; first three also shared external Neo4j cores'}
                if args.load == 'cubes' else None),
            'isolated_bird_proof_supplement': (
                {'root': str(BIRD_PROOF_SUPPLEMENT),
                 'selected_units': sum(path.is_relative_to(BIRD_PROOF_SUPPLEMENT)
                                       for _, _, path in expected),
                 'selection': 'seed1 shards2-5; seed2 shards0-5; seed3 shard0',
                 'original_core_conflict': list(range(169, 186))}
                if args.load == 'bird' else None),
            'id_files_sha256': {str(path): sha(path) for path in LOADS[args.load][2]},
            'scorer_sha256': sha(Path(__file__)),
            'definitions': {
                'repeats': 'same complete batch panel under arrival-order seeds 0-4',
                'pair_calls': 'each unresolved pair receives at most one canonical Polygon search',
                'censoring': 'full input retained; completion and unprocessed arrivals reported separately',
                'host_cpu': 'host parent and waited children; external MySQL CPU excluded',
            }}, sort_keys=True, indent=2) + '\n',
    }
    if output.exists():
        assert {path.name for path in output.iterdir()} == set(files)
        assert all((output / name).read_text() == content for name, content in files.items())
    else:
        pending = output.with_name(output.name + '.pending')
        assert not pending.exists()
        pending.mkdir(parents=True)
        for name, content in files.items():
            (pending / name).write_text(content)
        pending.rename(output)
    print(json.dumps({'state': 'published_complete_scores', 'units': len(rows),
                      'output': str(output), 'scores_sha256': sha(output / 'scores.json')},
                     sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
