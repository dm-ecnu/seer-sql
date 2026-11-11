"""C69 XData formal receipts versus unchanged XDataN, case preserved."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse,collections,csv,importlib.util,json,math,statistics,sys
from pathlib import Path
from receipts import read,sha,write,matrix,objective
R=Path((SEER_ROOT));P=R/'repro'
sys.path.insert(0,str(P/'c65-summary-repair'))
from c65_summary import paired_ratio,csv_write
FIELDS={'signature_index':'fingerprint_arrival_separated_pairs','refutation_engine':'quick_separated_pairs',
        'bounded_validation_library':'polygon_library_separated_pairs','seed_pool':'seed_pool_separated_pairs',
        'unlabelled_pool':'fingerprint_separated_pairs','residual_search':'search_separated_pairs'}
def norm(q):return ' '.join(q.split())
def summarize(formal,replay,out):
    plan,units=matrix(formal,'formal',700)
    batches={b['batch_id']:b for b in map(json.loads,(R/'datasets/batches/xdatabm.jsonl').read_text().splitlines())}
    assert len(batches)==84 and sum(len(b['queries'])-1 for b in batches.values())==414
    checked=read(replay);assert checked['complete'] and checked['source_commit']==plan['source_commit']
    cert={(u['batch'],u['seed']):u for u in checked['units'] if u['panel']=='xdatabm'}
    native=[r for r in map(json.loads,(P/'xdatan-native-home-check-v1/checked.jsonl').read_text().splitlines())
            if r.get('dataset')=='xdatabm' and r.get('type')=='batch']
    native_keys=set();native_cert=set()
    for r in native:
        sql={q['qid']:norm(q['sql']) for q in batches[r['batch_id']]['queries']}
        for pair in r['pairs']:
            k=(r['batch_id'],sql[pair['qid']])
            if pair['native']=='Killed':native_keys.add(k)
            if pair['genuine']:native_cert.add(k)
    assert len(native_keys)==370 and len(native_cert)==360
    rows=[];attrs=collections.defaultdict(list);killed=collections.defaultdict(set)
    labels=collections.defaultdict(set);legacy_kills=collections.defaultdict(set)
    for u in units:
        if u['status']=='complete':
            r=u['result'];m=r['metrics'];unsupported=bool(r.get('unsupported_reason'))
            counts={k:0 if unsupported else m[v] for k,v in FIELDS.items()}
            assert sum(counts.values())==r['pair_counts']['separated']
            attrs[u['panel']].append(dict(batch=u['batch'],seed=u['seed'],unsupported=unsupported,
                counts=counts,pool_size=m['pool_size'],active_pool_size=m['active_pool_size'],
                executed_queries=m['processed_queries']-m.get('unsupported_queries',0),pair_decisions=m['pair_decision_requests'],
                separated_pairs=r['pair_counts']['separated'],legacy_solver_plus_quick=m['solver_calls']+m.get('quick_witness_calls',0),
                source=u['path']+'/result.json',result_sha256=u['result_sha256'],config_sha256=r['config_sha256']))
        if u['panel']!='xdatabm':continue
        bid,seed=u['batch'],u['seed'];b=batches[bid]
        d=P/'dstM3-pair-xdata-v4/xdatabm'/bid/f'opponent-s{seed}'
        oc=read(d/'config.json');op=read(d/'result.json');om=op['metrics']
        assert op['config_sha256']==sha(d/'config.json') and oc['source_commit'].startswith('bb4b6bc')
        actual=sum(len(c['mutants']) for c in op['calls']);assert actual==om['native_pair_calls']
        row=dict(batch=bid,seed=seed,mutants=len(b['queries'])-1,status=u['status'],
            seer_wall_s=None,seer_pair_requests=None,seer_censored=None,seer_solver_plus_quick=None,
            xdatan_wall_s=om['wall_s'],xdatan_native_pair_requests=actual,xdatan_censored=om['censored_at_budget'],
            legacy_opponent_reference_pairs=max(0,om['processed_queries']-1),
            seer_budget_s=u['budget_s'],xdatan_budget_s=oc['budget_s'],seer_certified_kill_unique=None,
            seer_certified_case_preserving_kill_unique=None,seer_label_kill_unique=None,
            seer_result=u['path']+'/result.json',xdatan_result=str(d/'result.json'),
            seer_sha256=u.get('result_sha256'),xdatan_sha256=sha(d/'result.json'))
        if u['status']=='complete':
            r=u['result'];m=r['metrics'];c=cert[bid,seed]
            assert u['config']['query_ids']==oc['query_ids']==[q['qid'] for q in b['queries']]
            assert c['result_sha256']==u['result_sha256'] and c['status']=='complete'
            qs=set(c['certified_sql_case_preserving']);killed[seed].update((bid,q) for q in qs)
            legacy_kills[seed].update((bid,q.lower()) for q in qs)
            member={q:i for i,group in enumerate(r['classes']) for q in group}
            ref=next(q['qid'] for q in b['queries'] if q['role']=='reference')
            label={norm(q['sql']) for q in b['queries'] if q['role']=='candidate' and
                   ref in member and q['qid'] in member and member[ref]!=member[q['qid']]}
            assert qs<=label
            labels[seed].update((bid,q) for q in label)
            row.update(seer_wall_s=m['wall_s'],seer_pair_requests=m['pair_decision_requests'],
                seer_solver_plus_quick=m['solver_calls']+m.get('quick_witness_calls',0),
                seer_censored=m['censored_at_budget'],seer_certified_kill_unique=len(qs),
                seer_certified_case_preserving_kill_unique=len(qs),seer_label_kill_unique=len(label))
        rows.append(row)
    assert len(rows)==420 and len({(r['batch'],r['seed']) for r in rows})==420
    valid=[r for r in rows if r['status']=='complete' and not r['seer_censored'] and not r['xdatan_censored']]
    wall=paired_ratio(valid,'xdatan_wall_s','seer_wall_s','batch')
    requests=paired_ratio(valid,'xdatan_native_pair_requests','seer_pair_requests','batch')
    proxy=paired_ratio(valid,'legacy_opponent_reference_pairs','seer_solver_plus_quick','batch')
    points=[]
    for bid in sorted(batches):
        rs=[r for r in valid if r['batch']==bid]
        if not rs:continue
        points.append(dict(batch=bid,mutants=rs[0]['mutants'],seeds=len(rs),
            wall_ratio=math.exp(statistics.mean(math.log(r['xdatan_wall_s']/r['seer_wall_s']) for r in rs)),
            seer_median_wall_s=statistics.median(r['seer_wall_s'] for r in rs),
            xdatan_median_wall_s=statistics.median(r['xdatan_wall_s'] for r in rs)))
    attribution={}
    for panel,rs in attrs.items():
        sup=[r for r in rs if not r['unsupported']];counts={k:sum(r['counts'][k] for r in rs) for k in FIELDS}
        den=sum(counts.values());queries=sum(r['executed_queries'] for r in sup)
        attribution[panel]=dict(units=len(rs),supported_units=len(sup),unsupported_units=len(rs)-len(sup),counts=counts,
            separated_pairs=den,executed_queries=queries,pair_decisions=sum(r['pair_decisions'] for r in sup),
            shares={k:v/den if den else None for k,v in counts.items()},
            pool_size_mean=statistics.mean(r['pool_size'] for r in sup) if sup else None,
            active_pool_size_mean=statistics.mean(r['active_pool_size'] for r in sup) if sup else None,
            decisions_per_query=sum(r['pair_decisions'] for r in sup)/queries if queries else None,
            legacy_solver_plus_quick_per_query=sum(r['legacy_solver_plus_quick'] for r in sup)/queries if queries else None,
            receipts=rs)
    counts=[len(killed[s]) for s in range(5)]
    kill=dict(seer_certified_seed0=counts[0],seer_certified_per_seed=counts,
        seer_certified_case_preserving_seed0=counts[0],seer_certified_case_preserving_per_seed=counts,
        seer_label_per_seed=[len(labels[s]) for s in range(5)],
        seer_certified_legacy_lowercase_per_seed=[len(legacy_kills[s]) for s in range(5)],
        seer_certified_completed_units_per_seed=[sum(r['seed']==s and r['status']=='complete' for r in rows) for s in range(5)],
        certification_missing_units=sum(r['status']!='complete' for r in rows),
        seer_certified_mean_sd=dict(mean=statistics.mean(counts),sample_sd=statistics.stdev(counts),population_sd=statistics.pstdev(counts)),
        seer_certified_case_preserving_mean_sd=dict(mean=statistics.mean(counts),sample_sd=statistics.stdev(counts),population_sd=statistics.pstdev(counts)),
        xdatan_native_unique=370,xdatan_replay_unique=360,xdatan_paper_reported=382,
        certification_status='independent_double_replay_case_preserving',replay_receipt=str(replay),replay_sha256=sha(replay),
        normalization_caveat="Whitespace normalization preserves case. Historical SEER 379 lowercased whole SQL and merged q082 LIKE 'cs' / LIKE 'Cs'; it is not this denominator.")
    summary=dict(units=420,batches=84,seeds=5,failed_units=sum(r['status']!='complete' for r in rows),
        seer_censored=sum(bool(r['seer_censored']) for r in rows),xdatan_censored=sum(bool(r['xdatan_censored']) for r in rows),
        wall_ratio=wall,request_ratio=requests,legacy_table3_call_proxy_ratio=proxy,
        seer_faster_fraction=wall.get('numerator_larger_workloads',0)/wall['workloads'] if wall['workloads'] else None,
        seer_wall_median_s=statistics.median(r['seer_wall_s'] for r in rows if r['status']=='complete') if any(r['status']=='complete' for r in rows) else None,
        xdatan_wall_median_s=statistics.median(r['xdatan_wall_s'] for r in rows),
        size_bins=[dict(min_mutants=lo,max_mutants=hi,**paired_ratio([r for r in valid if lo<=r['mutants']<=hi],
            'xdatan_wall_s','seer_wall_s','batch')) for lo,hi in ((1,1),(2,3),(4,5),(6,8),(9,14))])
    obj=objective([u for u in units if u['panel']=='xdatabm'])
    table=dict(panel='XData-BM',batches=84,seeds=5,paired_units=len(valid),failed_units=summary['failed_units'],
        seer_censored=summary['seer_censored'],xdatan_censored=summary['xdatan_censored'],
        seer_certified_kill_seed0=counts[0],seer_kill_validation=kill['certification_status'],
        seer_label_kill_seed0=kill['seer_label_per_seed'][0],old_seer_certified_kill_seed0=379,
        xdatan_native_kill=370,xdatan_replay_kill=360,xdatan_paper_kill=382,
        seer_faster_batches=wall.get('numerator_larger_workloads',0),seer_faster_fraction=summary['seer_faster_fraction'],
        seer_median_workload_s=summary['seer_wall_median_s'],xdatan_median_workload_s=summary['xdatan_wall_median_s'],
        source_directory=str(formal),opponent_directory=str(P/'dstM3-pair-xdata-v4/xdatabm'),
        note='Ratios XDataN/SEER, non-adjacent periods. C69 main and case-preserving kill fields retain case; historical lowercase counts are separate in JSON.',
        seer_certified_mean=statistics.mean(counts),seer_certified_sample_sd=statistics.stdev(counts),
        seer_certified_population_sd=statistics.pstdev(counts),seer_certified_per_seed=json.dumps(counts),
        seer_certified_case_preserving_seed0=counts[0],seer_certified_case_preserving_per_seed=json.dumps(counts),
        seer_certified_case_preserving_mean=statistics.mean(counts),seer_certified_case_preserving_sample_sd=statistics.stdev(counts))
    for name in ('wall_ratio','request_ratio','legacy_table3_call_proxy_ratio'):
        v=summary[name];ci=v['ci95']
        table.update({name+'_geomean':v['geomean'],name+'_ci95_low':ci[0] if ci else None,
            name+'_ci95_high':ci[1] if ci else None,name+'_workloads':v['workloads'],name+'_zero_units_excluded':v['zero_pairs']})
    with (P/'c65-xdata-summary/tables/table-xdatabm-class.csv').open() as h:fields=csv.DictReader(h).fieldnames
    assert set(fields)==set(table), 'preserve the published C64 table columns'
    for name in ('rounds','extra_separations','candidates','extra_per_candidate'):
        table['objective_'+name]=obj[name]
    fields += [k for k in table if k not in fields]
    out.mkdir(parents=True,exist_ok=True);csv_write(out/'table-xdatabm-c69.csv',[table],fields)
    if points:csv_write(out/'fig6b-xdatabm-c69.csv',points,list(points[0]))
    if points:assert math.isclose(math.exp(statistics.mean(math.log(r['wall_ratio']) for r in points)),wall['geomean'],rel_tol=1e-12)
    write(out/'table-xdatabm-c69.json',dict(summary=summary,kill=kill,units=rows,attribution=attribution,table_row=table,
        objective=obj,
        provenance=dict(formal_source_directory=str(formal),source_commit=plan['source_commit'],budget_policy=plan['budget_policy'],
            opponent_source_directory=str(P/'dstM3-pair-xdata-v4/xdatabm'),script_sha256=sha(__file__),
            note='C69 SEER versus retained XDataN; not same-period adjacent. Failed runs retained; paired efficiency excludes censoring and zero calls.')))
    print(json.dumps(dict(summary=summary,kill=kill)))

if __name__=='__main__':
    p=argparse.ArgumentParser()
    for k in ('formal','replay','out'):p.add_argument('--'+k,type=Path,required=True)
    a=p.parse_args();summarize(a.formal,a.replay,a.out)
