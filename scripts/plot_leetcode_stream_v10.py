"""Render scale figures from the complete, validated LeetCode score directory.

The scorer withholds per-seed.csv until all frozen test units have arrived.
This script deliberately has no path that reads partial result directories.
"""
from __future__ import annotations

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse
import csv
import hashlib
import json
import math
import statistics
from bisect import bisect_right
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
STYLE = ROOT / 'figures/paper.mplstyle'
DEFAULT_SCORES = Path((SEER_ROOT + '/repro/stream-partition-v1/leetcode-scale-v10-final/scores-v1'))
METHODS = (
    ('seer_stream', 'SEER'),
    ('seer_stream_sqlwitness', 'SEER + SQLWitness'),
    ('sqlwitness', 'SQLWitness, all pairs'),
    ('sqlwitness_representative', 'SQLWitness, representatives'),
)
SIZES = (10, 25, 50, 100, 200, 500)
SEEDS = tuple(range(5))
DEFAULT_RAW = Path((SEER_ROOT + '/repro/stream-partition-v1'))
TIME_GRID = tuple(range(0, 601, 10))


def load_scores(directory: Path):
    manifest = json.loads((directory / 'scores.json').read_text())
    assert manifest['state'] == 'complete' and manifest['expected_units'] == 2160
    assert set(manifest['methods']) == {method for method, _ in METHODS}
    with (directory / 'per-seed.csv').open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(METHODS) * len(SIZES) * len(SEEDS)
    by_key = {}
    for row in rows:
        key = (row['method'], int(row['n']), int(row['seed']))
        assert key not in by_key
        assert int(row['batches']) == 18
        assert int(row['input_queries']) == 18 * key[1]
        assert int(row['complete_batches']) + int(row['censored_batches']) == 18
        by_key[key] = row
    expected = {(method, n, seed) for method, _ in METHODS for n in SIZES for seed in SEEDS}
    assert set(by_key) == expected
    return by_key, hashlib.sha256((directory / 'per-seed.csv').read_bytes()).hexdigest()


def series(rows, method: str, metric: str):
    means, stds = [], []
    for n in SIZES:
        values = [float(rows[method, n, seed][metric]) for seed in SEEDS]
        assert all(math.isfinite(v) and v >= 0 for v in values)
        means.append(statistics.mean(values))
        stds.append(statistics.stdev(values))
    return means, stds


def coverage_at(events: list[dict], n: int, grid=TIME_GRID):
    """Current certified separation coverage; a disabled witness may lower it."""
    assert n >= 2
    snapshots = [(0.0, 0)]
    for event in events:
        if event.get('event') not in ('arrival', 'pool_added'):
            continue
        t, separated = event['wall_s'], event['separated_pairs']
        assert 0 <= t < 600 and 0 <= separated <= n * (n-1) // 2
        assert t >= snapshots[-1][0]
        snapshots.append((t, separated))
    assert len(snapshots) > 1
    times = [item[0] for item in snapshots]
    counts = [item[1] for item in snapshots]
    denominator = n * (n-1) // 2
    return [counts[bisect_right(times, t)-1] / denominator for t in grid]


def load_coverage(directory: Path, raw_base: Path):
    with (directory / 'per-unit.csv').open(newline='') as stream:
        units = list(csv.DictReader(stream))
    selected = [row for row in units if int(row['n']) == 500]
    assert len(selected) == len(METHODS) * len(SEEDS) * 18
    cohorts = {method: ('leetcode-scale-v10-final' if method.startswith('seer_stream')
                        else 'leetcode-scale-v9') for method, _ in METHODS}
    curves = {}
    for row in selected:
        method, seed, bid = row['method'], int(row['seed']), row['batch_id']
        assert method in cohorts and seed in SEEDS
        key = method, seed, bid
        assert key not in curves
        path = raw_base / cohorts[method] / method / f's{seed}' / bid / 'result.json'
        content = path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == row['result_sha256'], path
        result = json.loads(content)
        assert result['pair_counts']['separated'] == int(row['separated_pairs'])
        curve = coverage_at(result['events'], 500)
        snapshots = [event['separated_pairs'] for event in result['events']
                     if event.get('event') in ('arrival', 'pool_added')]
        assert snapshots[-1] == result['pair_counts']['separated']
        curves[key] = curve
    grouped = {}
    for method, _ in METHODS:
        for seed in SEEDS:
            batch_curves = [curve for (m, s, _), curve in curves.items()
                            if m == method and s == seed]
            assert len(batch_curves) == 18
            grouped[method, seed] = [statistics.mean(values) for values in zip(*batch_curves)]
    return grouped, hashlib.sha256((directory / 'per-unit.csv').read_bytes()).hexdigest()


def decorate(ax, title: str, ylabel: str):
    ax.set_title(title)
    ax.set_xlabel('Queries in stream, $n$')
    ax.set_ylabel(ylabel)
    ax.set_xscale('log', base=2)
    ax.set_xticks(SIZES, [str(n) for n in SIZES])
    ax.set_xlim(8, 600)
    ax.set_ylim(bottom=0)
    ax.grid(axis='y', alpha=0.18)


def add_lines(ax, rows, metric: str, methods=METHODS):
    lines = []
    for method, label in methods:
        mean, sd = series(rows, method, metric)
        line, = ax.plot(SIZES, mean, label=label, linewidth=1.4, markersize=4.2)
        low = [max(0.0, m-s) for m, s in zip(mean, sd)]
        high = [m+s for m, s in zip(mean, sd)]
        ax.fill_between(SIZES, low, high, color=line.get_color(), alpha=0.10, linewidth=0)
        lines.append(line)
    return lines


def save(fig, path: Path, source_sha: str):
    fig.savefig(path, metadata={'Title': path.stem,
                                'Subject': 'Five arrival-order seeds; complete frozen test matrix',
                                'Keywords': 'per-seed.csv sha256 ' + source_sha},
                bbox_inches='tight')
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scores', type=Path, default=DEFAULT_SCORES)
    parser.add_argument('--raw-base', type=Path, default=DEFAULT_RAW)
    parser.add_argument('--outdir', type=Path, required=True)
    args = parser.parse_args()
    rows, source_sha = load_scores(args.scores)
    coverage, unit_sha = load_coverage(args.scores, args.raw_base)
    assert STYLE.is_file()
    args.outdir.mkdir(parents=True, exist_ok=True)
    scale = args.outdir / 'fig1-leetcode-scale.pdf'
    pool = args.outdir / 'fig2-pool-and-classes.pdf'
    coverage_path = args.outdir / 'fig3-coverage-time.pdf'
    assert not any(path.exists() for path in (scale, pool, coverage_path)), \
        'figures are immutable; use a new output directory'
    with plt.style.context(STYLE):
        fig, axes = plt.subplots(1, 3, figsize=(7.0, 2.65), layout='constrained')
        handles = add_lines(axes[0], rows, 'throughput_queries_s')
        decorate(axes[0], '(a) Throughput', 'Processed queries / s')
        add_lines(axes[1], rows, 'pair_decisions_per_processed_query')
        decorate(axes[1], '(b) Outer pair calls', 'Calls / processed query')
        add_lines(axes[2], rows, 'solver_checks_per_processed_query', METHODS[:2])
        decorate(axes[2], '(c) SEER solver work', 'SMT checks / processed query')
        fig.legend(handles, [label for _, label in METHODS], loc='outside upper center', ncols=4)
        save(fig, scale, source_sha)

        fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.45), layout='constrained')
        handles = add_lines(axes[0], rows, 'mean_pool_size')
        decorate(axes[0], '(a) Evidence pool', 'Databases in pool')
        add_lines(axes[1], rows, 'mean_class_count')
        decorate(axes[1], '(b) Fingerprint classes', 'Classes')
        fig.legend(handles, [label for _, label in METHODS], loc='outside upper center', ncols=4)
        save(fig, pool, source_sha)

        fig, ax = plt.subplots(figsize=(7.0, 2.55), layout='constrained')
        handles = []
        for method, label in METHODS:
            per_seed = [coverage[method, seed] for seed in SEEDS]
            means = [statistics.mean(values) for values in zip(*per_seed)]
            stds = [statistics.stdev(values) for values in zip(*per_seed)]
            line, = ax.plot(TIME_GRID, means, label=label, linewidth=1.4,
                            markersize=3.8, markevery=10)
            ax.fill_between(TIME_GRID,
                            [max(0., mean-sd) for mean, sd in zip(means, stds)],
                            [min(1., mean+sd) for mean, sd in zip(means, stds)],
                            color=line.get_color(), alpha=0.10, linewidth=0)
            handles.append(line)
        ax.set(xlim=(0, 600), ylim=(0, 1), xlabel='Elapsed time (s)',
               ylabel='Currently separated / all input pairs',
               title='LeetCode, $n=500$, common 600 s budget')
        ax.grid(axis='y', alpha=0.18)
        fig.legend(handles, [label for _, label in METHODS], loc='outside upper center', ncols=4)
        save(fig, coverage_path, unit_sha)
    print(json.dumps({'state': 'figures_from_complete_scores', 'input_sha256': source_sha,
                      'per_unit_sha256': unit_sha,
                      'figures': [str(scale), str(pool), str(coverage_path)]}, sort_keys=True))


if __name__ == '__main__':
    main()
