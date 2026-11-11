#!/usr/bin/env python3
"""Score the frozen BIRD public-pool SEER/official SQLWitness cohort.

No aggregate is emitted until every one of 1534 x 5 x 2 original receipts
is present, complete, and independently validated. Efficiency uses only the
predeclared 200-test-batch stratified sample.
"""
from __future__ import annotations

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
TMPDIR = os.environ.get("TMPDIR", "/tmp")
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

ROOT = Path((SEER_ROOT))
DATA = ROOT / 'datasets/batches/public-batch-v1'
RUN = ROOT / 'repro/bird-public-original-paired-v3'
SAMPLE = ROOT / 'repro/stream-partition-v1/paired-efficiency-sample-v1.json'
OUT = RUN / 'score-v1/summary.json'
COMMIT = 'b0c45c87ea6693f66dfe28250ab767c9360ee979'
INPUT_SHA = '22833c4c9bfb435af9a3833fa153eae07ef9a410c5a2b120d0ffc2795387b9f1'
SAMPLE_SHA = '570238e3dc81ae0dd2afa3023a1e6794d22c115b248178a2b04f2c02e511ea74'
METHODS = ('seer_stream', 'sqlwitness')
CORES = (5, 6, 11, 12, 14, 16, 17, 21, 29, 62, 152, 156, 159, 168, 189, 190)
SEEDS = range(5)
N = 11
PAIRS = N * (N - 1) // 2
BOOT_SEED = 20271028


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values, p):
    values = sorted(values)
    x = (len(values) - 1) * p
    lo, hi = math.floor(x), math.ceil(x)
    return values[lo] + (x - lo) * (values[hi] - values[lo])


def input_rows():
    assert sha(DATA / 'bird_public.jsonl') == INPUT_SHA
    rows = [json.loads(line) for line in (DATA / 'bird_public.jsonl').open()]
    source = {r['batch_id']: r for r in rows}
    assert len(rows) == len(source) == 1534
    test = (DATA / 'test.ids').read_text().splitlines()
    dev = (DATA / 'dev.ids').read_text().splitlines()
    assert len(test) == 1243 and len(dev) == 291
    assert set(test).isdisjoint(dev) and set(test + dev) == set(source)
    assert all(len(r['queries']) == N for r in rows)
    return source, test, dev


def sample_rows(test):
    sample = json.loads(SAMPLE.read_text())
    assert sample['seed'] == 20261027
    bird = sample['bird']
    assert bird['batch_list_sha256'] == SAMPLE_SHA
    rows = bird['rows']
    assert len(rows) == len({r['batch'] for r in rows}) == 200
    assert {r['batch'] for r in rows} <= set(test)
    assert all(r['design_weight'] > 0 and r['stratum'] for r in rows)
    return rows


def path(method, seed, batch):
    return RUN / method / f's{seed}' / batch


def pair_mask(result, ids):
    labels = {}
    for group, members in enumerate(result['classes']):
        for q in members:
            assert q in ids and q not in labels
            labels[q] = group
    assert len(labels) == result['metrics']['processed_queries']
    mask = 0
    k = 0
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a in labels and b in labels and labels[a] != labels[b]:
                mask |= 1 << k
            k += 1
    assert k == PAIRS and mask.bit_count() == result['pair_counts']['separated']
    return mask


def read_unit(source, method, seed, batch, core):
    folder = path(method, seed, batch)
    config_path, result_path = folder / 'config.json', folder / 'result.json'
    config = json.loads(config_path.read_text())
    result = json.loads(result_path.read_text())
    metrics = result['metrics']
    ids = [q['qid'] for q in source[batch]['queries']]
    arrivals = ids[:]
    random.Random(seed).shuffle(arrivals)
    observed = [e['query'] for e in result['events'] if e.get('event') == 'arrival']
    assert config['source_commit'] == COMMIT
    assert (config['batch'], config['method'], config['seed']) == (batch, method, seed)
    assert config['budget_s'] == 600 and config['order'] == 'shuffle'
    assert config['affinity'] == [core] and config['query_ids'] == ids
    assert config['method_seed'] == 0
    assert config['temporary_directory'] == (TMPDIR)
    assert result['config_sha256'] == sha(config_path)
    assert metrics['input_queries'] == N
    processed = metrics['processed_queries']
    assert processed + metrics['unprocessed_queries'] == N
    assert observed == arrivals[:processed]
    assert result['unprocessed_query_ids'] == arrivals[processed:]
    assert metrics['invalid_admitted_certificates'] == 0
    assert metrics['censored_at_budget'] == (not metrics['completed_within_budget'])
    counts = result['pair_counts']
    assert sum(counts.values()) == processed * (processed - 1) // 2
    if method == 'sqlwitness':
        assert config['native_mode'] == 'official_experiment_driver_one_process_per_batch'
        assert metrics['native_pair_calls'] == metrics['pair_decision_requests']
        assert counts['proved'] == counts['bounded'] == 0
    else:
        assert config['stream_schedule'] == 'tail_proof'
    mask = pair_mask(result, ids)
    peak = int(result['cgroup_resources']['memory_peak_bytes'])
    assert peak > 0 and 0 < metrics['wall_s'] <= 620
    return {'mask': mask, 'processed': processed, 'separated': mask.bit_count(),
            'proved': counts['proved'], 'bounded': counts['bounded'],
            'undecided': counts['undecided'], 'censored': bool(metrics['censored_at_budget']),
            'wall_s': metrics['wall_s'], 'requests': metrics['pair_decision_requests'],
            'db_hits': metrics['small_db_executions'], 'peak_mib': peak / 2**20,
            'result_sha256': sha(result_path)}


def bootstrap_difference(batch_rows, draws=10000):
    # Paired batch is the sampling unit; all five predeclared arrival seeds stay together.
    n = len(batch_rows)
    point = sum(x[0] for x in batch_rows) / sum(x[1] for x in batch_rows)
    rng = random.Random(BOOT_SEED)
    values = []
    for _ in range(draws):
        draw = [batch_rows[rng.randrange(n)] for _ in range(n)]
        values.append(sum(x[0] for x in draw) / sum(x[1] for x in draw))
    return {'difference': point, 'ci90': [percentile(values, .05), percentile(values, .95)],
            'bootstrap_batches': n, 'bootstrap_draws': draws, 'bootstrap_seed': BOOT_SEED,
            'noninferior_delta_1pp': percentile(values, .05) > -.01}


def efficiency(sample, unit, draws=4000):
    strata = defaultdict(list)
    for row in sample:
        bid = row['batch']
        vals = []
        req = []
        censored = False
        for seed in SEEDS:
            a, b = unit[bid, seed, 'seer_stream'], unit[bid, seed, 'sqlwitness']
            vals.append(math.log(b['wall_s'] / a['wall_s']))
            if a['requests'] and b['requests']:
                req.append(math.log(b['requests'] / a['requests']))
            censored |= a['censored'] or b['censored']
        strata[row['stratum']].append((row['design_weight'], statistics.mean(vals),
                                      statistics.mean(req) if len(req) == 5 else None,
                                      censored))
    def point(groups, metric, completed_only=False):
        pairs = [(w, x) for rows in groups.values() for w, wall, requests, censored in rows
                 if (not completed_only or not censored)
                 for x in [wall if metric == 'wall' else requests] if x is not None]
        if not pairs:
            return None
        weight = sum(w for w, _ in pairs)
        return {'geomean_ratio_opponent_over_seer': math.exp(sum(w * x for w, x in pairs) / weight),
                'seer_better_batch_fraction': sum(w * (x > 0) for w, x in pairs) / weight,
                'batches': len(pairs)}
    rng = random.Random(BOOT_SEED + 1)
    outputs = {}
    for metric, completed_only in (('wall', False), ('wall', True), ('requests', False)):
        label = 'completed_wall' if completed_only else metric
        observed = point(strata, metric, completed_only)
        boot = []
        for _ in range(draws):
            sampled = {s: [rows[rng.randrange(len(rows))] for _ in rows]
                       for s, rows in strata.items()}
            v = point(sampled, metric, completed_only)
            if v is not None:
                boot.append(v['geomean_ratio_opponent_over_seer'])
        outputs[label] = None if observed is None else {
            **observed, 'ci95': [percentile(boot, .025), percentile(boot, .975)],
            'bootstrap_draws': draws, 'bootstrap_seed': BOOT_SEED + 1}
    outputs['censored_sample_batches'] = sum(any(unit[r['batch'], seed, method]['censored']
                                                  for seed in SEEDS for method in METHODS)
                                             for r in sample)
    return outputs


def main():
    source, test, dev = input_rows()
    sample = sample_rows(test)
    expected = {(bid, seed, method) for bid in source for seed in SEEDS for method in METHODS}
    records = {}
    errors = []
    all_ids = test + dev
    for lane in range(len(CORES)):
        f = RUN / f'lane-{lane:02d}.jsonl'
        if not f.exists():
            continue
        selected = all_ids[lane::len(CORES)]
        expected_order = []
        for seed in SEEDS:
            for serial, bid in enumerate(selected):
                order = METHODS if (seed + serial) % 2 == 0 else METHODS[::-1]
                expected_order.extend((bid, seed, method) for method in order)
        lane_rows = [json.loads(line) for line in f.open()]
        assert len(lane_rows) <= len(expected_order)
        for index, row in enumerate(lane_rows):
            assert (row['batch'], row['seed'], row['method']) == expected_order[index]
            assert row['core'] == CORES[lane]
            key = (row['batch'], row['seed'], row['method'])
            assert key in expected and key not in records
            records[key] = row
            if row['status'] not in ('complete', 'already_complete'):
                errors.append(row)
    missing = expected - records.keys()
    if missing or errors:
        print(json.dumps({'state': 'incomplete_no_scores_published',
                          'expected': len(expected), 'receipts': len(records),
                          'failed': len(errors), 'first_failed': errors[:2],
                          'first_missing': sorted(missing)[:3]}))
        return 2
    assert len(records) == len(expected) == 15340
    unit = {}
    for bid, seed, method in sorted(expected):
        record = records[bid, seed, method]
        row = read_unit(source, method, seed, bid, record['core'])
        assert row['result_sha256'] == record['result_sha256']
        assert row['processed'] == record['processed_queries']
        unit[bid, seed, method] = row
    known = {}
    for bid in source:
        union = 0
        for seed in SEEDS:
            for method in METHODS:
                union |= unit[bid, seed, method]['mask']
        known[bid] = union.bit_count()
    effect_rows = []
    for bid in test:
        sep_a = sum(unit[bid, seed, 'seer_stream']['separated'] for seed in SEEDS)
        sep_b = sum(unit[bid, seed, 'sqlwitness']['separated'] for seed in SEEDS)
        effect_rows.append((sep_a - sep_b, known[bid] * len(SEEDS)))
    assert sum(den for _, den in effect_rows) > 0
    totals = {}
    per_seed = {}
    for method in METHODS:
        per_seed[method] = []
        for seed in SEEDS:
            rows = [unit[bid, seed, method] for bid in test]
            per_seed[method].append({k: sum(x[k] for x in rows) for k in
                                     ('processed', 'separated', 'proved', 'bounded', 'undecided',
                                      'requests', 'db_hits')})
        rows = [unit[bid, seed, method] for bid in test for seed in SEEDS]
        totals[method] = {k: sum(x[k] for x in rows) for k in
                          ('processed', 'separated', 'proved', 'bounded', 'undecided',
                           'requests', 'db_hits')}
        totals[method]['censored_units'] = sum(x['censored'] for x in rows)
        totals[method]['known_neq_recall'] = totals[method]['separated'] / (
            sum(known[bid] for bid in test) * len(SEEDS))
        totals[method]['per_seed_separated_mean'] = statistics.mean(
            x['separated'] for x in per_seed[method])
        totals[method]['per_seed_separated_sd'] = statistics.stdev(
            x['separated'] for x in per_seed[method])
    output = {'state': 'complete', 'input_sha256': INPUT_SHA,
              'sample_sha256': SAMPLE_SHA, 'method_commit': COMMIT,
              'test_batches': len(test), 'dev_batches': len(dev),
              'seeds': list(SEEDS), 'units': len(unit),
              'test_known_neq_pairs': sum(known[bid] for bid in test),
              'test_method_totals': totals,
              'paired_noninferiority': bootstrap_difference(effect_rows),
              'paired_efficiency_test_200': efficiency(sample, unit),
              'scope': 'effect uses all 1243 public test batches x 5 seeds; efficiency uses frozen 200 stratified test batches x 5 seeds; observed wall ratio includes censored units and completed_wall excludes them; known NEQ is union of both methods over all five seeds'}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(OUT)
    print(json.dumps({'state': 'complete', 'output': str(OUT), 'units': len(unit)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
