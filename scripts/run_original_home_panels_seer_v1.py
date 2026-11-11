"""Run SEER on published Spider/BIRD same-reference panels, preserving every source pair."""

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
TMPDIR = os.environ.get("TMPDIR", "/tmp")
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import threading
import time

ROOT = Path((SEER_ROOT))
TREE = ROOT / 'seer-original-home-panels-wt'
DATA = ROOT / 'datasets/batches'
OUT = ROOT / 'repro/original-home-seer-v1'
PANELS = (
    ('spider_sqlwitness_original_batches', 60, 564, 1034),
    ('bird_sqlwitness_original_batches', 60, 1528, 1534),
    ('bird_parseval_original_batches', 360, 1526, 1534),
)
CORES = tuple(range(121, 129))
LOCK = threading.Lock()


def source_tasks():
    tasks = []
    for panel, deadline, expected_batches, expected_pairs in PANELS:
        source = DATA / (panel + '.jsonl')
        batches = [json.loads(line) for line in source.open()]
        assert len(batches) == expected_batches
        assert sum(len(b['queries']) - 1 for b in batches) == expected_pairs
        assert len({b['batch_id'] for b in batches}) == expected_batches
        for batch in batches:
            assert batch['dataset'] == panel
            assert sum(q.get('role') == 'reference' for q in batch['queries']) == 1
            assert sum(q.get('role') == 'candidate' for q in batch['queries']) == len(batch['queries'])-1
            tasks.append((panel, deadline, source, batch))
    return tasks


def run_one(task, core):
    panel, deadline, source, batch = task
    bid = batch['batch_id']
    pairs = len(batch['queries']) - 1
    group = OUT / panel
    output = group / bid
    log = group / 'logs' / (bid + '.log')
    if (output / 'result.json').exists():
        return {'panel': panel, 'batch': bid, 'status': 'already_complete'}
    if output.exists():
        return {'panel': panel, 'batch': bid, 'status': 'prior_incomplete'}
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(TMPDIR=(TMPDIR),
               SQLITE_TMPDIR=(TMPDIR),
               SEER_SQLWITNESS_SQLITE_SCRATCH=(TMPDIR),
               OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1',
               MKL_NUM_THREADS='1', MALLOC_ARENA_MAX='2',
               PYTHONPATH=f'{TREE / "src"}:{TREE / "eval"}')
    budget = pairs * deadline
    command = ['taskset', '-c', str(core), 'timeout', '--signal=TERM',
               '--kill-after=5s', f'{budget + 120}s',
               str(ROOT / 'venv-seer/bin/python'), 'scripts/run_stream_batch.py',
               '--dataset', str(source), '--batch', bid,
               '--method', 'seer_stream', '--schedule', 'tail_proof',
               '--original-pair-timeout-s', str(deadline),
               '--seed', '0', '--order', 'source', '--out', str(output)]
    start = time.time()
    with log.open('w') as stream:
        proc = subprocess.run(command, cwd=TREE, env=env,
                              stdout=stream, stderr=subprocess.STDOUT)
    record = {'panel': panel, 'batch': bid, 'core': core,
              'original_pairs': pairs, 'pair_timeout_s': deadline,
              'exit_code': proc.returncode, 'elapsed_s': time.time()-start,
              'status': 'failed'}
    if proc.returncode == 0 and (output / 'result.json').exists():
        result = json.loads((output / 'result.json').read_text())
        config = json.loads((output / 'config.json').read_text())
        metrics = result['metrics']
        if (config['original_reference_pairs'] == pairs
                and config['original_pair_timeout_s'] == deadline
                and config['budget_s'] == budget
                and metrics['input_queries'] == pairs + 1
                and metrics['processed_queries'] + metrics['unprocessed_queries'] == pairs + 1
                and metrics['invalid_admitted_certificates'] == 0):
            record['status'] = 'complete'
            record['method_wall_s'] = metrics['wall_s']
            record['processed_queries'] = metrics['processed_queries']
        else:
            record['status'] = 'invalid_result'
    with LOCK:
        with (OUT / 'status.jsonl').open('a') as sink:
            sink.write(json.dumps(record, separators=(',', ':')) + '\n')
    return record


def lane(items, core):
    for task in items:
        record = run_one(task, core)
        if record['status'] not in ('complete', 'already_complete'):
            print(json.dumps(record), flush=True)


def main():
    tasks = source_tasks()
    OUT.mkdir(parents=True, exist_ok=True)
    print(json.dumps({'tasks': len(tasks), 'panels': PANELS, 'cores': CORES,
                      'source_commit': subprocess.check_output(
                          ['git', 'rev-parse', 'HEAD'], cwd=TREE, text=True).strip()}), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(CORES)) as pool:
        futures = [pool.submit(lane, tasks[i::len(CORES)], core)
                   for i, core in enumerate(CORES)]
        for future in concurrent.futures.as_completed(futures):
            future.result()
    print(json.dumps({'finished': len(tasks)}), flush=True)


if __name__ == '__main__':
    main()
