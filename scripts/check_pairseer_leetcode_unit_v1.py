"""Check one B600 PairSeer unit without dropping deadlines or source failures."""
from __future__ import annotations

import os
TMPDIR = os.environ.get("TMPDIR", "/tmp")
import argparse
import hashlib
import json
from pathlib import Path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('unit', type=Path)
    args = parser.parse_args()
    unit = args.unit
    config = json.loads((unit / 'config.json').read_text())
    rows = (unit / 'raw.jsonl').read_text().splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert config['source_commit'] == 'd4ff8d2a4d4d83c8f4dd6cd6a72f686605d2b4a7'
    assert config['auxiliary_commit'] == (unit.parents[2] / 'launcher-commit.txt').read_text().strip()
    assert (config['method'], config['method_seed'], config['budget_s'], config['per_call_s']) == \
        ('pair_seer', 0, 600, 60)
    assert config['arrival_seed'] in range(5) and len(config['affinity']) == 1
    assert config['temporary_directory'] == (TMPDIR)
    assert sha(Path(config['dataset'])) == config['dataset_sha256']
    manifest = json.loads((unit.parents[2] / 'input/arrival-inputs.json').read_text())
    assert manifest['outputs'][str(config['arrival_seed'])]['sha256'] == config['dataset_sha256']
    assert sha(Path(config['only'])) == config['only_sha256']
    assert Path(config['only']).read_text().strip() == config['batch']
    assert (row['batch_id'], row['tool'], row['seed'], row['budget']) == \
        (config['batch'], 'pair_seer', 0, 600)
    contract = row['comparison_contract']
    assert contract['hard_parent_receipt_deadline'] is True
    assert contract['fresh_common_dbms_runs'] == 2
    assert contract['goal_policy'] == 'all_pairs'
    assert contract['files'][str(Path(config['dataset']).resolve())] == config['dataset_sha256']
    assert contract['files'][str(Path(config['only']).resolve())] == config['only_sha256']
    assert row['service_cleanup']['verified']
    assert all(entry['verified'] for entry in row['external_backend_cleanup'])
    assert all(receipt['received_s'] < 600 for receipt in row['accepted_receipts'])
    pairs = row['pairs']
    assert len(pairs) == 500 * 499 // 2
    assert len({tuple(sorted((pair['a'], pair['b']))) for pair in pairs}) == len(pairs)
    assert sum(pair['verdict'] == 'split' for pair in pairs) == row['n_genuine_pairs']
    assert 0 <= row['n_genuine_pairs'] <= len(pairs)
    print(json.dumps({'batch': config['batch'], 'arrival_seed': config['arrival_seed'],
                      'source_status': row['source_status'], 'source_error': row['runner_error'],
                      'genuine_pairs': row['n_genuine_pairs'], 'databases': row['n_dbs'],
                      'accepted_receipts': len(row['accepted_receipts']),
                      'raw_sha256': sha(unit / 'raw.jsonl')}, sort_keys=True))


if __name__ == '__main__':
    main()
