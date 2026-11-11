"""Score frozen Spider/BIRD paper panels without changing their source or test denominators."""

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path((SEER_ROOT))
DATA = ROOT / 'datasets/batches'
RUNS = ROOT / 'repro/original-home-seer-v1'
OUT = ROOT / 'repro/original-panels-score-v1'
PAIRED = ROOT / 'seer-original-pair-panel-wt/scripts/score_leetcode_verieql_original_v1.py'
MASK = ROOT / 'official-cert-wt/scripts/matrix_score.py'
PANELS = (
    ('spider_sqlwitness_original_batches', 'spider', 'sqlwitness_index', 0, 60, 564, 1034, 829),
    ('bird_sqlwitness_original_batches', 'bird', 'sqlwitness_index', 0, 60, 1528, 1534, 1226),
    ('bird_parseval_original_batches', 'bird', 'parseval_index', 1, 360, 1526, 1534, 1240),
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def score_one(batch, result, config, pair_timeout, index_key, offset, base, valid_ids, helper, mask):
    queries = batch['queries']
    reference = [q for q in queries if q.get('role') == 'reference']
    candidates = [q for q in queries if q.get('role') == 'candidate']
    assert len(reference) == 1 and len(candidates) + 1 == len(queries)
    assert config['query_ids'] == [q['qid'] for q in queries]
    assert config['original_reference_pairs'] == len(candidates)
    assert config['original_pair_timeout_s'] == pair_timeout
    assert config['per_call_s'] == pair_timeout
    assert config['budget_s'] == len(candidates) * pair_timeout
    assert result['contract'] == 'canonical-6sig-list-bag-errors-v1'
    metrics = result['metrics']
    assert metrics['input_queries'] == len(queries)
    assert metrics['processed_queries'] + metrics['unprocessed_queries'] == len(queries)
    assert metrics['invalid_admitted_certificates'] == 0
    processed = set(config['query_ids']) - set(result['unprocessed_query_ids'])
    membership = {}
    for group_id, group in enumerate(result['classes']):
        for qid in group:
            assert qid not in membership
            membership[qid] = group_id
    assert set(membership) == processed
    gold = reference[0]['qid']
    guarantees = helper.gold_guarantees(result, gold)
    rows = []
    for candidate in candidates:
        qid = candidate['qid']
        index = candidate[index_key] + offset
        source_id = f'{base}-p{index:04d}'
        if gold not in processed or qid not in processed:
            status = 'unprocessed'
        elif membership[gold] != membership[qid]:
            status = 'refuted'
        elif qid in guarantees and guarantees[qid] == float('inf'):
            status = 'proved'
        elif qid in guarantees:
            status = 'bounded'
        elif (result['relations'].get(qid, {}).get('status') == 'unsupported' or
              result['relations'].get(gold, {}).get('status') == 'unsupported'):
            status = 'unsupported'
        else:
            status = 'undecided'
        rows.append({'index': index, 'source_id': source_id, 'batch': batch['batch_id'],
                     'status': status, 'valid_original_input': source_id in valid_ids,
                     'test': source_id in valid_ids and not mask.is_dev(base+'_pairs', source_id)})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--panel', choices=[entry[0] for entry in PANELS],
                        help='score one complete published panel')
    args = parser.parse_args()
    helper = load_module('original_pair_score', PAIRED)
    mask = load_module('original_panel_mask', MASK)
    OUT.mkdir(parents=True, exist_ok=True)
    for panel, base, key, offset, timeout, n_batches, n_pairs, n_test in PANELS:
        if args.panel and panel != args.panel:
            continue
        source = DATA / (panel + '.jsonl')
        batches = [json.loads(line) for line in source.open()]
        assert len(batches) == n_batches
        valid_source = DATA / (base + '_pairs.jsonl')
        valid_batches = [json.loads(line) for line in valid_source.open()]
        valid_ids = {b['batch_id'] for b in valid_batches}
        if panel == 'bird_parseval_original_batches':
            # ParSEval's published BIRD source retains all 1,534 cases. The
            # SQLWitness validity mask describes a different source panel.
            valid_ids = {f'bird-p{i:04d}' for i in range(1, 1535)}
        rows = []
        commits = set()
        method_snapshots = set()
        wall_s = 0.
        censored = 0
        for batch in batches:
            folder = RUNS / panel / batch['batch_id']
            result_path = folder / 'result.json'
            config_path = folder / 'config.json'
            if not result_path.exists() or not config_path.exists():
                raise FileNotFoundError(f'incomplete original panel: {folder}')
            result = json.loads(result_path.read_text())
            config = json.loads(config_path.read_text())
            rows.extend(score_one(batch, result, config, timeout, key, offset,
                                  base, valid_ids, helper, mask))
            commits.add(config['source_commit'])
            method_files = {
                path: digest for path, digest in config['files'].items()
                if path.startswith(str(ROOT / 'seer-original-home-panels-wt/src/'))
                or path.startswith(str(ROOT / 'seer-original-home-panels-wt/eval/'))
                or path == str(ROOT / 'seer-original-home-panels-wt/scripts/run_stream_batch.py')
                or path == str(ROOT / 'seer-original-home-panels-wt/scripts/sqlwitness_batch_worker.py')
            }
            assert any(path.endswith('/scripts/run_stream_batch.py') for path in method_files)
            method_snapshots.add(hashlib.sha256(json.dumps(
                method_files, sort_keys=True).encode()).hexdigest())
            wall_s += result['metrics']['wall_s']
            censored += bool(result['metrics']['censored_at_budget'])
        assert len(method_snapshots) == 1, 'method implementation changed within one panel'
        assert len(rows) == n_pairs and len({row['index'] for row in rows}) == n_pairs
        test = [row for row in rows if row['test']]
        assert len(test) == n_test
        cross_panel_reference_mismatch = None
        if panel == 'bird_parseval_original_batches':
            sqlwitness_source = DATA / 'bird_sqlwitness_original_batches.jsonl'
            witness_references = {
                q['sqlwitness_index']: (b['meta']['db_id'], b['queries'][0]['sql'])
                for b in map(json.loads, sqlwitness_source.open())
                for q in b['queries'] if q.get('role') == 'candidate'
            }
            cross_panel_reference_mismatch = sum(
                witness_references[q[key] + offset] !=
                (b['meta']['db_id'], b['queries'][0]['sql'])
                for b in batches for q in b['queries'] if q.get('role') == 'candidate')
            assert cross_panel_reference_mismatch == 200
        summary = {'panel': panel, 'source_commits': sorted(commits),
                   'method_files_sha256': method_snapshots.pop(),
                   'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                   'split_mask_source_sha256': hashlib.sha256(valid_source.read_bytes()).hexdigest(),
                   'batches': n_batches, 'all_original_pairs': n_pairs,
                   'valid_original_pairs': len(valid_ids), 'test_pairs': n_test,
                   'cross_panel_reference_mismatch': cross_panel_reference_mismatch,
                   'all_counts': dict(Counter(row['status'] for row in rows)),
                   'test_counts': dict(Counter(row['status'] for row in test)),
                   'pair_timeout_s': timeout, 'censored_batches': censored,
                   'all_wall_s': wall_s, 'wall_s_per_all_original_pair': wall_s/n_pairs,
                   'cases': rows}
        (OUT / (panel + '.json')).write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps({k:v for k,v in summary.items() if k != 'cases'}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
