"""Score SEER only on the published LeetCode gold/candidate pairs.

The stream may also classify candidate/candidate pairs. Those are deliberately
excluded from this original-panel denominator and from every category below.
"""
from __future__ import annotations

import argparse
from collections import Counter
import heapq
import json
from pathlib import Path


def gold_guarantees(result, gold):
    """Best certified path from gold: proved beats any bounded path."""
    graph = {}
    domain = result.get('domain_identity')
    for edge in result.get('evidence_edges', []):
        if edge['status'] == 'proved':
            strength = float('inf')
        elif edge['status'] == 'bounded' and edge.get('domain') == domain:
            strength = int(edge['k'])
        else:
            continue
        a, b = edge['a'], edge['b']
        graph.setdefault(a, []).append((b, strength))
        graph.setdefault(b, []).append((a, strength))
    best = {gold: float('inf')}
    queue = [(-float('inf'), gold)]
    while queue:
        neg_strength, node = heapq.heappop(queue)
        strength = -neg_strength
        if strength < best[node]:
            continue
        for other, edge_strength in graph.get(node, ()):
            gained = min(strength, edge_strength)
            if gained > best.get(other, 0):
                best[other] = gained
                heapq.heappush(queue, (-gained, other))
    return best


def score_batch(batch, result, config, pair_timeout_s):
    queries = batch['queries']
    gold = [q for q in queries if q.get('role') == 'reference']
    candidates = [q for q in queries if q.get('role') == 'candidate']
    count = batch['meta']['original_pair_count']
    if len(gold) != 1 or len(candidates) != count or len(queries) != count + 1:
        raise ValueError('original reference/candidate denominator changed')
    if config['query_ids'] != [q['qid'] for q in queries]:
        raise ValueError('runner input order differs from the original panel')
    if (config.get('original_pair_timeout_s') != pair_timeout_s or
            config.get('per_call_s') != pair_timeout_s or
            config.get('budget_s') != count * pair_timeout_s):
        raise ValueError('runner did not use the original pair deadline')
    metrics = result['metrics']
    if (metrics['input_queries'] != len(queries) or
            metrics['processed_queries'] + metrics['unprocessed_queries'] != len(queries) or
            metrics['invalid_admitted_certificates'] != 0):
        raise ValueError('incomplete or invalid batch result')
    if result['contract'] != 'canonical-6sig-list-bag-errors-v1':
        raise ValueError('comparison contract changed')
    processed = set(config['query_ids']) - set(result['unprocessed_query_ids'])
    membership = {}
    for index, group in enumerate(result['classes']):
        for q in group:
            if q in membership:
                raise ValueError('query appears in two classes')
            membership[q] = index
    if set(membership) != processed:
        raise ValueError('result classes do not cover exactly the processed queries')
    gold_id = gold[0]['qid']
    guarantees = gold_guarantees(result, gold_id)
    rows = []
    for candidate in candidates:
        q = candidate['qid']
        k = None
        if gold_id not in processed or q not in processed:
            status = 'unprocessed'
        elif membership[gold_id] != membership[q]:
            status = 'refuted'
        elif q in guarantees and guarantees[q] == float('inf'):
            status = 'proved'
        elif q in guarantees:
            status, k = 'bounded', guarantees[q]
        elif (result['relations'].get(q, {}).get('status') == 'unsupported' or
              result['relations'].get(gold_id, {}).get('status') == 'unsupported'):
            status = 'unsupported'
        else:
            status = 'undecided'
        rows.append({'problem': batch['meta']['problem'],
                     'original_query_id': candidate['original_query_id'],
                     'status': status, 'bounded_k': k})
    counts = Counter(row['status'] for row in rows)
    if sum(counts.values()) != count:
        raise AssertionError('original panel denominator lost')
    return rows, counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--results-dir', type=Path, required=True)
    parser.add_argument('--pair-timeout-s', type=float, choices=(60., 600.), required=True)
    parser.add_argument('--batch', help='diagnose one original problem only')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    batches = [json.loads(line) for line in args.dataset.read_text().splitlines()]
    if args.batch:
        batches = [b for b in batches if b['batch_id'] == args.batch]
        if len(batches) != 1:
            raise ValueError('expected one requested batch')
    elif len(batches) != 56:
        raise ValueError('original LeetCode panel is not 56 problems')
    source_commits = set()
    all_rows = []
    all_counts = Counter()
    wall_s = 0.
    censored_batches = 0
    for batch in batches:
        path = args.results_dir / batch['batch_id']
        config = json.loads((path / 'config.json').read_text())
        result = json.loads((path / 'result.json').read_text())
        rows, counts = score_batch(batch, result, config, args.pair_timeout_s)
        all_rows.extend(rows)
        all_counts.update(counts)
        wall_s += result['metrics']['wall_s']
        censored_batches += bool(result['metrics']['censored_at_budget'])
        source_commits.add(config['source_commit'])
    if len(source_commits) != 1:
        raise ValueError('mixed implementation commits in one score')
    expected = sum(b['meta']['original_pair_count'] for b in batches)
    if len(all_rows) != expected or (not args.batch and expected != 23994):
        raise ValueError('published 23,994-pair denominator changed')
    score = {'source_commit': source_commits.pop(), 'pair_timeout_s': args.pair_timeout_s,
             'problems': len(batches), 'original_pairs': expected,
             'counts': dict(all_counts), 'censored_batches': censored_batches,
             'total_wall_s': wall_s, 'mean_wall_s_per_original_pair': wall_s / expected,
             'cases': all_rows}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(score, indent=2) + '\n')
    print(json.dumps({k: v for k, v in score.items() if k != 'cases'}, sort_keys=True))


if __name__ == '__main__':
    main()
