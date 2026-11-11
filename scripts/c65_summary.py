"""Two paper tables from immutable c64/m36 receipts and adjacent c65 ablations."""
from __future__ import annotations
import argparse
import collections
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys
sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'src')]
from seer.class_metrics import depth_counts
from c63_consistency import P, read, write, sha
from c65_ablation import BASE, VARIANTS, FORMAL
from score_paired_intervals_v1 import percentile

OLD_FIELDS = ['problem','seed','queries','budget_s','seer_status','seer_within_budget',
    'seer_wall_s','seer_pair_decision_requests','sqlwitness_status',
    'sqlwitness_native_complete','sqlwitness_wall_s','sqlwitness_requested_pairs']
TIMES = ['separation_wall_s','proof_wall_s','finalization_wall_s']
COUNT_FIELDS = ['block_queries','sat','unsat','timeout','unknown','unsupported','other']
NOTE = ('SEER class method uses c64 frozen reruns; SQLWitness reuses m36 native receipts. '
        'These methods were not run adjacently in the same time period. '
        'm36 SEER is the previous implementation, not the class_off ablation.')


def csv_write(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='') as h:
        w = csv.DictWriter(h, fieldnames=fields, extrasaction='raise')
        w.writeheader(); w.writerows(rows)


def paired_ratio(rows, numerator, denominator, group='problem'):
    """Equal workload weights; seeds remain together in 10,000 cluster draws."""
    grouped = collections.defaultdict(list); zero = 0
    for r in rows:
        a, b = r.get(numerator), r.get(denominator)
        if a is None or b is None or a == '' or b == '': continue
        a, b = float(a), float(b)
        assert math.isfinite(a) and math.isfinite(b) and a >= 0 and b >= 0
        if not a or not b:
            zero += 1
            continue
        grouped[r[group]].append(math.log(a/b))
    values = [statistics.mean(v) for _, v in sorted(grouped.items())]
    if not values:
        return dict(geomean=None, ci95=None, workloads=0, paired_units=0, zero_pairs=zero)
    rng = random.Random(0)
    draws = [math.exp(statistics.mean(rng.choices(values, k=len(values)))) for _ in range(10000)]
    return dict(geomean=math.exp(statistics.mean(values)),
        ci95=[percentile(draws,.025), percentile(draws,.975)], workloads=len(values),
        numerator_larger_workloads=sum(v>0 for v in values),
        paired_units=sum(map(len,grouped.values())), zero_pairs=zero,
        bootstrap_draws=10000, bootstrap_seed=0, unit='workload with its valid seeds kept together')


def detailed(r):
    counts = depth_counts(r['events'], r['metrics'].get('class_checks',0))
    if 'class_depth_counts' in r: assert counts == r['class_depth_counts']
    assert set(r['tiers']) == set(TIMES)
    assert all(math.isfinite(r['tiers'][k]) and r['tiers'][k] >= 0 for k in TIMES)
    m = r['metrics']
    return dict(**r['tiers'], class_depth_counts=json.dumps(counts,sort_keys=True,separators=(',',':')),
        **{'class_'+k:sum(c[k] for c in counts.values()) for k in COUNT_FIELDS},
        solver_calls=m['solver_calls'], solver_calls_engine_reported=m.get('solver_calls_engine_reported'),
        pair_decision_requests=m.get('pair_decision_requests',0),
        separated_pairs=r['pair_counts']['separated'],proved_pairs=r['pair_counts']['proved'],
        bounded_pairs=r['pair_counts']['bounded'],undecided_pairs=r['pair_counts']['undecided'])


def ablation_table(rows):
    """One row per intervention; five seed totals use balanced workloads."""
    by={(r['batch'],r['seed'],r['variant']):r for r in rows}
    assert len(by)==len(rows)==2520
    result=[]
    for variant in VARIANTS:
        units=[r for r in rows if r['variant']==variant]
        assert len(units)==420 and len({r['batch'] for r in units})==84
        complete=[r for r in units if r['status']=='complete']
        balanced=[b for b in sorted({r['batch'] for r in units})
                  if all(by[b,s,variant]['status']=='complete' for s in range(5))]
        matched=[]
        for r in complete:
            f=by[r['batch'],r['seed'],'full']
            if f['status']!='complete' or r['censored'] or f['censored']:continue
            matched.append(dict(batch=r['batch'],wall=r['wall_s'],full_wall=f['wall_s'],
                blocks=r['class_block_queries'],full_blocks=f['class_block_queries'],
                solver=r['solver_calls'],full_solver=f['solver_calls']))
        out=dict(panel='XData-BM',variant=variant,expected_runs=420,observed_runs=len(complete),
            failed_runs=420-len(complete),censored_runs=sum(bool(r['censored']) for r in complete),
            complete_five_seed_batches=len(balanced),matched_uncensored_runs=len(matched),
            source_commit=units[0]['source_commit'],objective_scope=units[0]['objective_scope'])
        for field in ('pair_decision_requests','solver_calls','class_block_queries',
                      'separated_pairs','proved_pairs','bounded_pairs','undecided_pairs'):
            seed_totals=[sum(by[b,s,variant][field] for b in balanced) for s in range(5)]
            out[field+'_total_observed']=sum(r[field] for r in complete)
            out[field+'_balanced_seed_mean']=statistics.mean(seed_totals) if balanced else None
            out[field+'_balanced_seed_sample_sd']=statistics.stdev(seed_totals) if balanced else None
        for field in ['wall_s',*TIMES]:
            out[field+'_per_unit_mean']=statistics.mean(r[field] for r in complete) if complete else None
        for label,num,den in (('wall','wall','full_wall'),('block_query','blocks','full_blocks'),
                              ('solver_call','solver','full_solver')):
            ratio=paired_ratio(matched,num,den,'batch');ci=ratio['ci95']
            out[label+'_ratio_vs_full']=ratio['geomean']
            out[label+'_ratio_ci95_low']=ci[0] if ci else None
            out[label+'_ratio_ci95_high']=ci[1] if ci else None
            out[label+'_ratio_workloads']=ratio['workloads']
            out[label+'_ratio_zero_pairs_excluded']=ratio['zero_pairs']
        depths=collections.defaultdict(collections.Counter)
        for r in complete:
            for k,c in json.loads(r['class_depth_counts']).items():
                depths[k].update({n:c[n] for n in COUNT_FIELDS})
        out['class_depth_counts_observed']=json.dumps(dict(depths),sort_keys=True,separators=(',',':'))
        result.append(out)
    assert len(result)==6 and sum(r['expected_runs'] for r in result)==2520
    return result


def legacy_row(job, snapshot):
    bid, seed = job['batch'], job['seed']; problem = bid.rsplit('-',1)[-1]
    if (problem,seed) in snapshot: return dict(snapshot[problem,seed])
    # m36 was still closing five units when the original 275-row table was
    # exported. Missing rows use only existing m36 terminal/native receipts.
    root = P/'dstM3-sqlite-m36'
    cfg = read(job['config']); out = root/'main/leetcode'/bid
    row = dict(problem=problem, seed=seed, queries=len(cfg['query_ids']), budget_s=cfg['budget_s'],
        seer_status='pending',seer_within_budget=0,seer_wall_s=None,seer_pair_decision_requests=None,
        sqlwitness_status='pending',sqlwitness_native_complete=0,sqlwitness_wall_s=None,sqlwitness_requested_pairs=None)
    t = root/'terminal'/f'main--leetcode--{bid}--{seed}.json'
    terminal = read(t)['records'] if t.exists() else {}
    for who in ('seer','opponent'):
        d = out/f'{who}-s{seed}'; f = d/'result.json'
        status = terminal.get(who,{}).get('status','pending')
        row['seer_status' if who=='seer' else 'sqlwitness_status'] = status
        if who=='seer' and f.exists():
            r=read(f);m=r['metrics'];row.update(seer_wall_s=m['wall_s'],
                seer_pair_decision_requests=m.get('pair_decision_requests'),
                seer_within_budget=int(m['completed_within_budget']))
        if who=='opponent':
            n=d/'native-receipt.json'
            if n.exists():
                native=read(n)
                if native['state']=='native_complete':
                    assert native['config_sha256']==sha(d/'config.json')
                    row.update(sqlwitness_native_complete=1,sqlwitness_wall_s=native['official_batch_wall_s'],
                        sqlwitness_requested_pairs=native['official_driver_requested_pairs'])
            elif f.exists():
                r=read(f);m=r['metrics']
                row.update(sqlwitness_native_complete=int(not m.get('censored_at_budget',True)),
                    sqlwitness_wall_s=m.get('wall_s'),sqlwitness_requested_pairs=m.get('pair_decision_requests'))
    return row


def summarize(ablation, old_csv, output):
    formal = FORMAL/'formal'; fc = read(formal/'config.json'); fr = read(formal/'result.json')
    ac = read(ablation/'config.json'); ar = read(ablation/'result.json')
    assert fc['source_commit']==BASE and len(fc['jobs'])==fr['total']==700
    assert fr['passed'] and len(fr['completed'])==700 and not fr['failed'], 'formal queue not fully successful'
    assert not ac['development_only'] and ac['units']==ar['total']==2520
    assert ar['complete'] and len(ar['records'])==2520, 'ablation has missing runs'
    assert len(ac['groups'])==420 and set(ac['variants'])==set(VARIANTS)
    with old_csv.open() as h:
        reader=csv.DictReader(h);assert reader.fieldnames==OLD_FIELDS
        legacy={(r['problem'],int(r['seed'])):r for r in reader}
    lc=[]; depth_rows=[]; four=[]
    for job in fc['jobs']:
        d=formal/'formal'/job['panel']/job['batch']/f'seer-s{job["seed"]}'
        c=read(d/'config.json');r=read(d/'result.json');m=r['metrics']
        assert c['source_commit']==BASE and r['config_sha256']==sha(d/'config.json')
        assert c['seed']==job['seed'] and c['batch']==job['batch']
        assert m['invalid_admitted_certificates']==0
        detail=detailed(r)
        depth_rows.append(dict(panel=job['panel'],batch=job['batch'],seed=job['seed'],**detail))
        if job['panel']!='leetcode':continue
        row=legacy_row(job,legacy)
        assert int(row['queries'])==len(c['query_ids']) and float(row['budget_s'])==c['budget_s']
        row.update(previous_implementation_status=row['seer_status'],
            previous_implementation_wall_s=row['seer_wall_s'],
            previous_implementation_requests=row['seer_pair_decision_requests'])
        row.update(seer_status='complete',seer_within_budget=int(m['completed_within_budget']),
            seer_wall_s=m['wall_s'],seer_pair_decision_requests=m.get('pair_decision_requests',0),
            **detail,source_commit=BASE,source_result=str(d/'result.json'),table_note=NOTE)
        lc.append(row)
        if row['problem'] in ('1132','1251','1661','1777'):
            four.append(dict(problem=row['problem'],seed=job['seed'],within_budget=bool(m['completed_within_budget']),
                unprocessed=m['unprocessed_queries'],**r['tiers']))
    assert len(lc)==280 and len(depth_rows)==700 and len(four)==20
    assert len({(r['problem'],int(r['seed'])) for r in lc})==280
    assert len({r['problem'] for r in lc})==56
    valid=[r for r in lc if int(r['seer_within_budget']) and int(r['sqlwitness_native_complete'])]
    lsummary=dict(total_units=280,total_workloads=56,valid_units=len(valid),
        status_counts=dict(collections.Counter(r['sqlwitness_status'] for r in lc)),
        seer_censored=sum(not r['seer_within_budget'] for r in lc),
        request_ratio=paired_ratio(valid,'sqlwitness_requested_pairs','seer_pair_decision_requests'),
        wall_ratio=paired_ratio(valid,'sqlwitness_wall_s','seer_wall_s'),four_capped_problems=four)
    rows=[]; grouped=collections.defaultdict(dict)
    for record in ar['records']:
        row=dict(batch=record['batch'],seed=record['seed'],variant=record['variant'],
            status=record['status'],returncode=record['returncode'],core=record['core'],
            started=record['started'],finished=record['finished'],
            source_commit=ac['source_commit'],wall_s=None,censored=None,source_result=record['path']+'/result.json',
            objective_scope=ac['objective_off_scope'] if record['variant']=='objective_off' else '')
        if record['status']=='complete':
            d=Path(record['path']);c=read(d/'config.json');r=read(d/'result.json');m=r['metrics']
            assert c['source_commit']==ac['source_commit'] and c['affinity']==[record['core']]
            assert r['config_sha256']==sha(d/'config.json') and m['invalid_admitted_certificates']==0
            row.update(wall_s=m['wall_s'],censored=bool(m['censored_at_budget']),**detailed(r))
        key=(row['batch'],row['seed'])
        assert row['variant'] not in grouped[key]
        grouped[key][row['variant']]=row;rows.append(row)
    assert len(rows)==2520 and len(grouped)==420
    assert collections.Counter(r['variant'] for r in rows)=={v:420 for v in VARIANTS}
    for variants in grouped.values():
        assert set(variants)==set(VARIANTS) and len({r['core'] for r in variants.values()})==1
        ordered=sorted(variants.values(),key=lambda r:r['started'])
        assert all(a['finished']<=b['started'] for a,b in zip(ordered,ordered[1:]))
    paired=[]
    for (batch,seed),v in grouped.items():
        a,b=v['class_off'],v['full']
        if any(r['status']!='complete' or r['censored'] for r in (a,b)):continue
        paired.append(dict(batch=batch,seed=seed,off_wall=a['wall_s'],full_wall=b['wall_s'],
            off_blocks=a['class_block_queries'],full_blocks=b['class_block_queries'],
            off_solver=a['solver_calls'],full_solver=b['solver_calls']))
    asummary=dict(total_units=2520,failures=sum(r['status']!='complete' for r in rows),
        censored=sum(bool(r['censored']) for r in rows),valid_full_class_off_units=len(paired),
        wall_ratio=paired_ratio(paired,'off_wall','full_wall','batch'),
        block_query_ratio=paired_ratio(paired,'off_blocks','full_blocks','batch'),
        solver_call_ratio=paired_ratio(paired,'off_solver','full_solver','batch'),
        objective_off_scope=ac['objective_off_scope'])
    output.mkdir(parents=True,exist_ok=True)
    extras=[k for k in lc[0] if k not in OLD_FIELDS]
    csv_write(output/'table3-leetcode-class.csv',sorted(lc,key=lambda r:(int(r['problem']),int(r['seed']))),OLD_FIELDS+extras)
    aggregate_rows=ablation_table(rows)
    csv_write(output/'table-ablation-xdatabm-class.csv',aggregate_rows,list(aggregate_rows[0]))
    # Machine-readable companion to the two tables, including XData's formal
    # per-depth counts; no raw result.json is edited or reclassified.
    write(output/'table3-leetcode-class.json',dict(summary=lsummary,formal_depth_rows=depth_rows,
        note=NOTE,old_table_sha256=sha(old_csv),formal_gate_sha256=sha(formal/'result.json')))
    write(output/'table-ablation-xdatabm-class.json',dict(summary=asummary,
        rows=aggregate_rows,units=rows,source_commit=ac['source_commit'],result_sha256=sha(ablation/'result.json')))
    print(json.dumps(dict(leetcode=lsummary,xdatabm=asummary),ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--ablation',type=Path,required=True)
    p.add_argument('--old-csv',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();summarize(a.ablation,a.old_csv,a.out)
