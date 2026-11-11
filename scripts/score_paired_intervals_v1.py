#!/usr/bin/env python3
"""Predeclared cluster intervals for a complete LeetCode main-table score.

The randomization unit is the held-out parent batch. All five arrival seeds
move together in each bootstrap draw; method rows stay paired throughout.
"""

import argparse
import csv
import json
import math
from pathlib import Path
import random
import statistics


TREATMENTS = ('seer_stream', 'seer_stream_sqlwitness')
RIVALS = ('sqlwitness', 'sqlwitness_representative', 'parseval', 'xdatan', 'pair_seer')
METHODS = TREATMENTS + RIVALS
SEEDS = tuple(range(5))
BOOTSTRAPS = 10000
RNG_SEED = 20271027


def percentile(values, fraction):
    assert values and 0 <= fraction <= 1
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (index - lower) * (ordered[upper] - ordered[lower])


def normalize(rows):
    data = {}
    batches = set()
    for row in rows:
        batch = row['batch_id']
        seed = int(row['seed'])
        method = row['method']
        assert method in METHODS and seed in SEEDS
        key = (batch, seed, method)
        assert key not in data
        separated = int(row['separated_pairs'])
        known = int(row['known_neq_pairs'])
        wall = float(row['wall_s'])
        assert 0 <= separated <= known and known > 0 and 0 < wall < math.inf
        data[key] = (separated, known, wall)
        batches.add(batch)
    batches = tuple(sorted(batches))
    assert len(batches) == 18
    assert set(data) == {(batch, seed, method)
                         for batch in batches for seed in SEEDS for method in METHODS}
    for batch in batches:
        expected = {data[batch, seed, method][1]
                    for seed in SEEDS for method in METHODS}
        assert len(expected) == 1, (batch, expected)
    return batches, data


def estimate(sample, data, treatment):
    recall = {}
    for method in METHODS:
        numerator = denominator = 0
        for batch in sample:
            for seed in SEEDS:
                separated, known, _ = data[batch, seed, method]
                numerator += separated
                denominator += known
        recall[method] = numerator / denominator
    strongest = max(RIVALS, key=lambda method: recall[method])
    noninferiority_gap = recall[treatment] - recall[strongest]
    speedups = {}
    for rival in RIVALS:
        logs = [math.log(data[batch, seed, rival][2] /
                         data[batch, seed, treatment][2])
                for batch in sample for seed in SEEDS]
        speedups[rival] = math.exp(statistics.mean(logs))
    median_wall = statistics.median(data[batch, seed, treatment][2]
                                    for batch in sample for seed in SEEDS)
    return strongest, noninferiority_gap, speedups, median_wall


def infer(rows, bootstraps=BOOTSTRAPS, rng_seed=RNG_SEED):
    assert bootstraps >= 1000
    batches, data = normalize(rows)
    rng = random.Random(rng_seed)
    draws = [[batches[rng.randrange(len(batches))] for _ in batches]
             for _ in range(bootstraps)]
    method_walls = {}
    for method in METHODS:
        point = statistics.median(data[batch, seed, method][2]
                                  for batch in batches for seed in SEEDS)
        sampled = [statistics.median(data[batch, seed, method][2]
                                     for batch in draw for seed in SEEDS)
                   for draw in draws]
        method_walls[method] = {
            'median_wall_s': point,
            'median_wall_ci95_s': [percentile(sampled, .025), percentile(sampled, .975)],
        }
    output = {}
    for treatment in TREATMENTS:
        strongest, point_gap, point_speeds, point_wall = estimate(batches, data, treatment)
        gaps = []
        walls = []
        speedups = {rival: [] for rival in RIVALS}
        for sample in draws:
            _, gap, speeds, wall = estimate(sample, data, treatment)
            gaps.append(gap)
            walls.append(wall)
            for rival in RIVALS:
                speedups[rival].append(speeds[rival])
        output[treatment] = {
            'strongest_rival_point': strongest,
            'recall_gap_to_bootstrap_strongest': point_gap,
            'recall_gap_ci90': [percentile(gaps, .05), percentile(gaps, .95)],
            'noninferior_delta_1pp': percentile(gaps, .05) > -.01,
            'median_wall_s': point_wall,
            'median_wall_ci95_s': [percentile(walls, .025), percentile(walls, .975)],
            'comparisons': {
                rival: {'geometric_speedup': point_speeds[rival],
                        'geometric_speedup_ci95': [percentile(speedups[rival], .025),
                                                   percentile(speedups[rival], .975)],
                        'p_seer_faster': sum(data[batch, seed, treatment][2] <
                                            data[batch, seed, rival][2]
                                            for batch in batches for seed in SEEDS) / (len(batches)*len(SEEDS))}
                for rival in RIVALS},
        }
    return {'state': 'complete', 'batch_clusters': len(batches), 'seeds_per_batch': len(SEEDS),
            'bootstrap_draws': bootstraps, 'rng_seed': rng_seed,
            'unit': 'parent batch, with all five arrival seeds paired across methods',
            'strongest_rival_rule': 'max competitor recall reselected in each bootstrap draw',
            'method_wall_intervals': method_walls, 'results': output}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scores', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    metadata = json.loads((args.scores / 'scores.json').read_text())
    assert metadata['state'] == 'complete' and metadata['expected_units'] == 630
    with (args.scores / 'per-unit.csv').open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 630
    result = infer(rows)
    content = json.dumps(result, sort_keys=True, indent=2) + '\n'
    if args.output.exists():
        assert args.output.read_text() == content
    else:
        args.output.write_text(content)
    print(json.dumps({'state': 'published_complete_intervals',
                      'batch_clusters': result['batch_clusters'], 'units': len(rows)}))


if __name__ == '__main__':
    main()
