"""Freeze per-batch arrival permutations for the legacy PairSeer runner.

Its single --seed controls native generation randomness, so the formal runner
uses --seed 0 and reads these query-order variants instead.  The schema, SQL,
query IDs, batch membership, and held-out denominator remain unchanged.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
from pathlib import Path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source: Path, ids: Path, output: Path) -> dict:
    requested = [line.strip() for line in ids.read_text().splitlines() if line.strip()]
    assert requested and len(set(requested)) == len(requested)
    rows = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    by_id = {row['batch_id']: row for row in rows}
    assert len(by_id) == len(rows) and set(requested) <= set(by_id)
    selected = [by_id[bid] for bid in requested]
    output.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for seed in range(5):
        transformed = []
        for row in selected:
            item = copy.deepcopy(row)
            original = item['queries']
            original_map = {query['qid']: query['sql'] for query in original}
            assert len(original) == len(original_map)
            item['schema_path'] = str((source.parent / item['schema_path']).resolve())
            assert Path(item['schema_path']).is_file()
            random.Random(seed).shuffle(item['queries'])
            assert {q['qid']: q['sql'] for q in item['queries']} == original_map
            transformed.append(item)
        target = output / f'arrivals-s{seed}.jsonl'
        content = ''.join(json.dumps(row, sort_keys=True) + '\n' for row in transformed)
        if target.exists():
            assert target.read_text() == content, target
        else:
            target.write_text(content)
        outputs[str(seed)] = {'path': str(target), 'sha256': sha(target)}
    contract = {'source': str(source.resolve()), 'source_sha256': sha(source),
                'ids': str(ids.resolve()), 'ids_sha256': sha(ids),
                'batches': len(selected), 'seeds': list(range(5)),
                'arrival_rule': 'random.Random(seed).shuffle, independently per batch',
                'legacy_method_seed': 0, 'outputs': outputs}
    manifest = output / 'arrival-inputs.json'
    content = json.dumps(contract, sort_keys=True, indent=2) + '\n'
    if manifest.exists():
        assert manifest.read_text() == content, manifest
    else:
        manifest.write_text(content)
    return contract


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--ids', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.ids, args.output), sort_keys=True))


if __name__ == '__main__':
    main()
