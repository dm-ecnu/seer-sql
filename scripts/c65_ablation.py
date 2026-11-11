"""Six adjacent XData variants on the existing shared physical-core leases."""
from __future__ import annotations
import concurrent.futures as cf
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from c63_consistency import R, P, NEW, PY, read, write, sha
from c64_pipeline import environment, available

BASE = '25fe74e117387693195d5ffb5ca6995eed7eacab'
FORMAL = P/'c64-class-v5'
VARIANTS = {'full': [], 'index_off': ['--ablation', 'no_fingerprint_grouping'],
    'pool_off': ['--ablation', 'no_pool_reuse'],
    'transfer_off': ['--no-certificate-transfer'],
    'objective_off': ['--single-bucket-target'], 'class_off': ['--class-off']}


def commit():
    assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=NEW, text=True).strip()
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=NEW, text=True).strip()
    subprocess.run(['git', 'merge-base', '--is-ancestor', BASE, head], cwd=NEW, check=True)
    return head


def jobs():
    frozen = read(FORMAL/'freeze.json')
    assert frozen['source_commit'] == BASE
    assert frozen['gate_sha256'] == sha(FORMAL/'development/result.json')
    assert read(FORMAL/'development/result.json')['passed']
    cfg = read(FORMAL/'formal/config.json')
    assert cfg['source_commit'] == BASE and len(cfg['jobs']) == 700
    selected = [j for j in cfg['jobs'] if j['panel'] == 'xdatabm']
    assert len(selected) == 420 and len({j['batch'] for j in selected}) == 84
    assert {(j['batch'], j['seed']) for j in selected} == {
        (b, s) for b in {j['batch'] for j in selected} for s in range(5)}
    return selected


def variant_order(index, seed):
    names = list(VARIANTS)
    offset = (index + seed) % len(names)
    return names[offset:] + names[:offset]


def group(root, index):
    plan = read(root/'config.json'); item = plan['groups'][index]; j = item['job']
    assert commit() == plan['source_commit']
    core, = os.sched_getaffinity(0)
    assert sha(j['input']) == j['input_sha256'] and sha(j['config']) == j['old_config_sha256']
    cfg = read(j['config']); assert cfg['per_call_s'] == 120
    env = environment(); records = []
    for variant in item['order']:
        out = root/'runs'/j['batch']/f's{j["seed"]}'/variant
        out.parent.mkdir(parents=True, exist_ok=True)
        assert not out.exists(), 'immutable attempt already exists'
        command = ['prlimit', '--as=6442450944', '--', 'timeout', '--signal=TERM',
            '--kill-after=5s', str(cfg['budget_s']+240), str(PY),
            str(NEW/'scripts/run_stream_batch.py'), '--dataset', j['input'],
            '--batch', j['batch'], '--method', 'seer_stream_sqlwitness',
            '--seed', str(j['seed']), '--order', cfg['order'], '--schedule', 'class_bounded',
            '--quick-schedule', 'compare', '--original-pair-timeout-s', str(cfg['per_call_s']),
            '--out', str(out), *VARIANTS[variant]]
        started = time.time()
        log = root/'logs'/f'{j["batch"]}-s{j["seed"]}-{variant}.log'
        with log.open('x') as h:
            rc = subprocess.call(command, env=env, cwd=NEW, stdout=h, stderr=subprocess.STDOUT)
        record = dict(batch=j['batch'], seed=j['seed'], variant=variant, core=core,
            started=started, finished=time.time(), returncode=rc, path=str(out), status='failed')
        if rc == 0:
            try:
                c = read(out/'config.json'); r = read(out/'result.json')
                assert c['source_commit'] == plan['source_commit'] and c['affinity'] == [core]
                assert c['budget_s'] == cfg['budget_s'] and c['per_call_s'] == cfg['per_call_s']
                assert c['query_ids'] == cfg['query_ids'] and c['seed'] == j['seed']
                assert r['config_sha256'] == sha(out/'config.json')
                assert r['metrics']['invalid_admitted_certificates'] == 0
                assert set(r['tiers']) == {'separation_wall_s','proof_wall_s','finalization_wall_s'}
                assert 'class_depth_counts' in r and c['class_off'] == (variant == 'class_off')
                assert r['metrics']['processed_queries']+r['metrics']['unprocessed_queries'] == len(c['query_ids'])
                record.update(status='complete', wall_s=r['metrics']['wall_s'],
                    censored=bool(r['metrics']['censored_at_budget']))
            except Exception as exc:
                record.update(status='invalid_result', error=repr(exc))
        records.append(record)
        write(root/'terminal'/f'{j["batch"]}-s{j["seed"]}-{variant}.json', record)
    assert len(records) == 6 and {r['core'] for r in records} == {core}
    assert all(a['finished'] <= b['started'] for a,b in zip(records,records[1:]))
    write(root/'groups'/f'{index}.json', dict(job=j, order=item['order'], core=core, records=records))


def run(root, pilot=False):
    root.mkdir(parents=True, exist_ok=False)
    for name in ('logs', 'terminal', 'groups'): (root/name).mkdir()
    selected = jobs()
    if pilot: selected = selected[:1]
    head = commit()
    groups = [dict(job=j, order=variant_order(i//5, j['seed'])) for i, j in enumerate(selected)]
    plan = dict(source_commit=head, base_commit=BASE, groups=groups, units=len(groups)*6,
        variants=list(VARIANTS), development_only=pilot, max_concurrent=4,
        memory_reserve_gib=128, per_lane_reserve_gib=24,
        objective_off_scope='legacy augmentation is inactive in frozen class full; non-identifying control',
        pairing='six variants adjacent on one leased physical core; rotation fixed by workload and seed')
    write(root/'config.json', plan)
    active = {}; cursor = 0; completed = []; errors = []
    def launch(i):
        env = environment()
        env['SEER_PROTECT_CORE_ROOT'] = str(FORMAL/'formal/formal')
        env['SEER_IDLE_MEMORY_GIB'] = '128'
        cmd = [str(PY), str(NEW/'scripts/c63_idle_run.py'), str(PY), str(Path(__file__).resolve()),
               'group', str(root), str(i)]
        with (root/'logs'/f'group-{i}.log').open('x') as h:
            return subprocess.call(cmd, cwd=NEW, env=env, stdout=h, stderr=subprocess.STDOUT)
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        while active or cursor < len(groups):
            capacity = min(4, max(0, int((available()-128)//24)))
            while cursor < len(groups) and len(active) < capacity:
                active[pool.submit(launch, cursor)] = dict(index=cursor, started=time.time())
                cursor += 1
            done, _ = cf.wait(active, timeout=15, return_when=cf.FIRST_COMPLETED) if active else ([], [])
            for future in done:
                info = active.pop(future)
                try: rc = future.result()
                except Exception as exc: rc=-1; info['error']=repr(exc)
                info['returncode'] = rc
                (completed if rc == 0 else errors).append(info)
            records = [read(f) for f in (root/'terminal').glob('*.json')]
            write(root/'status.json', dict(state='running', total=plan['units'],
                completed=sum(r['status']=='complete' for r in records),
                failures=[r for r in records if r['status']!='complete'], terminal_units=len(records),
                finished_groups=len(completed), group_errors=errors, active=list(active.values()),
                mem_available_gib=available()))
            if not active and cursor < len(groups): time.sleep(15)
    records = [read(f) for f in (root/'terminal').glob('*.json')]
    write(root/'result.json', dict(source_commit=head, total=plan['units'], records=records,
        group_errors=errors, complete=len(records)==plan['units'],
        passed=len(records)==plan['units'] and not errors and all(r['status']=='complete' for r in records)))


if __name__ == '__main__':
    if sys.argv[1] == 'group': group(Path(sys.argv[2]), int(sys.argv[3]))
    else: run(Path(sys.argv[2]), pilot=sys.argv[1]=='pilot')
