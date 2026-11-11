"""C69 LC and ablation tables, using the established C65 estimators."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse,collections,csv,importlib.util,json,statistics,sys
from pathlib import Path
from receipts import read,sha,write,matrix,objective
R=Path((SEER_ROOT));P=R/'repro';W=R/'seer-c69-objective-code'
sys.path.insert(0,str(P/'c65-summary-repair'))
import c65_summary as old
# The estimator remains unchanged; event accounting must match the new receipts.
spec=importlib.util.spec_from_file_location('c69_depth_counts',W/'src/seer/class_metrics.py')
metrics=importlib.util.module_from_spec(spec);spec.loader.exec_module(metrics)
old.depth_counts=metrics.depth_counts
NOTE=('C69 partition-wide objective and class-level SEER rerun. SQLWitness uses unchanged m36 receipts, '
      'not adjacent in the same time period. Previous-implementation values are m36 SEER.')

def summarize(formal,ablation,legacy_csv,replay,out):
    fp,fr=matrix(formal,'formal',700);ap,ar=matrix(ablation,'ablation',2520)
    assert fp['source_commit']==ap['source_commit'] and fp['budget_policy']==ap['budget_policy']
    jobs={(g['batch'],g['seed']):g for g in fp['groups']}
    with legacy_csv.open() as h:
        rd=csv.DictReader(h);assert rd.fieldnames==old.OLD_FIELDS
        legacy={(r['problem'],int(r['seed'])):r for r in rd}
    lc=[];depths=[]
    for unit in fr:
        job=jobs[unit['batch'],unit['seed']]
        detail={};result=unit.get('result');m=result['metrics'] if result else {}
        if result:
            detail=old.detailed(result)
            detail.update(objective_rounds=m.get('objective_rounds',0),
                objective_extra_separations=m.get('objective_extra_separations',0),
                objective_candidates=m.get('objective_candidates',0))
        depths.append(dict(panel=unit['panel'],batch=unit['batch'],seed=unit['seed'],status=unit['status'],
            censored=unit.get('censored'),source_result=unit['path']+'/result.json',**detail))
        if unit['panel']!='leetcode':continue
        row=old.legacy_row(job,legacy)
        previous=dict(status=row['seer_status'],wall=row['seer_wall_s'],within=int(row['seer_within_budget']),
                      requests=row['seer_pair_decision_requests'])
        row.update(previous_implementation_status=previous['status'],previous_implementation_wall_s=previous['wall'],
            previous_implementation_within_budget=previous['within'],previous_implementation_requests=previous['requests'],
            sqlwitness_budget_s=row['budget_s'],budget_s=unit['budget_s'],
            seer_status=('unsupported' if result and result.get('unsupported_reason') else unit['status']),
            seer_within_budget=int(m.get('completed_within_budget',False)),seer_wall_s=m.get('wall_s'),
            seer_pair_decision_requests=m.get('pair_decision_requests'),
            source_commit=fp['source_commit'],source_result=unit['path']+'/result.json',table_note=NOTE,**detail)
        lc.append(row)
    assert len(lc)==280 and len({(r['problem'],r['seed']) for r in lc})==280
    assert len({r['problem'] for r in lc})==56
    replayed=read(replay);assert replayed['complete'] and replayed['source_commit']==fp['source_commit']
    checked={(u['batch'].rsplit('-',1)[-1],u['seed']):u for u in replayed['units'] if u['panel']=='leetcode'}
    previous_detection=P/'c65-leetcode-detection/tables/table3-leetcode-detection.json'
    detection=read(previous_detection);assert detection['complete'] and detection['solver_calls']==0
    opponents={(u['batch'].rsplit('-',1)[-1],u['seed']):u for u in detection['units'] if u['method']=='SQLWitness'}
    for row in lc:
        key=(row['problem'],int(row['seed']));op=opponents[key]
        assert all(sha(p)==h for p,h in op['source_sha256'].items()),'retained opponent receipt changed'
        row.update(seer_certified_pairs=None,sqlwitness_certified_pairs=op['certified_pairs'],
                   supported_reference_unique_pairs=op['supported_reference_unique_pairs'])
        if row['seer_status'] in ('complete','unsupported'):
            new=checked[key];assert new['result_sha256']==sha(row['source_result'])
            assert new['supported_reference_unique_pairs']==op['supported_reference_unique_pairs']
            row['seer_certified_pairs']=len(new['certified_sql_case_preserving'])
    detected={}
    for method,col in (('SEER','seer_certified_pairs'),('SQLWitness','sqlwitness_certified_pairs')):
        totals=[sum(r[col] for r in lc if int(r['seed'])==s and r[col] is not None) for s in range(5)]
        detected[method]=dict(seed0=totals[0],per_seed=totals,mean=statistics.mean(totals),sample_sd=statistics.stdev(totals),
                             missing_units=sum(r[col] is None for r in lc))
    legacy_fields=old.OLD_FIELDS+['previous_implementation_status','previous_implementation_wall_s',
        'previous_implementation_within_budget','previous_implementation_requests',*old.TIMES,
        'tier_timing_source','tier_timing_status','class_depth_counts',
        *['class_'+k for k in old.COUNT_FIELDS],'solver_calls','solver_calls_engine_reported','pair_decision_requests',
        'separated_pairs','proved_pairs','bounded_pairs','undecided_pairs','unsupported_reason',
        'source_commit','source_result','table_note']
    fields=legacy_fields+sorted(set().union(*(r.keys() for r in lc))-set(legacy_fields))
    lc=[{k:r.get(k) for k in fields} for r in lc]
    valid=[r for r in lc if r['seer_status']=='complete' and r['seer_within_budget']
           and int(r['sqlwitness_native_complete']) and not r.get('unsupported_reason')]
    common=[r for r in valid if r['previous_implementation_within_budget'] and
            r['previous_implementation_wall_s'] not in (None,'') and r['previous_implementation_requests'] not in (None,'')]
    ls=dict(total_units=280,total_workloads=56,valid_units=len(valid),
        status_counts=dict(collections.Counter(r['sqlwitness_status'] for r in lc)),old_new_common_units=len(common),
        failed_units=sum(r['seer_status'] not in ('complete','unsupported') for r in lc),
        unsupported_units=sum(r['seer_status']=='unsupported' for r in lc),
        seer_censored=sum(r['seer_status']=='complete' and not r['seer_within_budget'] for r in lc),
        request_ratio=old.paired_ratio(valid,'sqlwitness_requested_pairs','seer_pair_decision_requests'),
        wall_ratio=old.paired_ratio(valid,'sqlwitness_wall_s','seer_wall_s'),
        previous_implementation_request_ratio=old.paired_ratio(common,'sqlwitness_requested_pairs','previous_implementation_requests'),
        previous_implementation_wall_ratio=old.paired_ratio(common,'sqlwitness_wall_s','previous_implementation_wall_s'),
        new_common_request_ratio=old.paired_ratio(common,'sqlwitness_requested_pairs','seer_pair_decision_requests'),
        new_common_wall_ratio=old.paired_ratio(common,'sqlwitness_wall_s','seer_wall_s'),
        previous_over_new_request_ratio=old.paired_ratio(common,'previous_implementation_requests','seer_pair_decision_requests'),
        previous_over_new_wall_ratio=old.paired_ratio(common,'previous_implementation_wall_s','seer_wall_s'),
        four_capped_problems=[r for r in lc if r['problem'] in ('1132','1251','1661','1777')],
        objective=objective([u for u in fr if u['panel']=='leetcode']),detection=detected,
        detection_paper_original=detection['paper_original'],detection_caveats=detection['caveats'])
    rows=[]
    for u in ar:
        row={k:u[k] for k in ('batch','seed','variant','status','returncode','core','started','finished')}
        row.update(source_commit=ap['source_commit'],wall_s=None,censored=None,
            source_result=u['path']+'/result.json',objective_scope='SAT model extension disabled; other stages unchanged' if u['variant']=='objective_off' else '')
        if u['status']=='complete':
            r=u['result'];m=r['metrics'];row.update(wall_s=m['wall_s'],censored=m['censored_at_budget'],**old.detailed(r))
        rows.append(row)
    aggregate=old.ablation_table(rows)
    for row in aggregate:row['objective']=json.dumps(objective([u for u in ar if u['variant']==row['variant']]),sort_keys=True)
    lookup={(r['batch'],r['seed'],r['variant']):r for r in rows};matched=[]
    for r in rows:
        if r['variant']!='class_off':continue
        f=lookup[r['batch'],r['seed'],'full']
        if any(x['status']!='complete' or x['censored'] for x in (r,f)):continue
        matched.append(dict(batch=r['batch'],off_wall=r['wall_s'],full_wall=f['wall_s'],
            off_blocks=r['class_block_queries'],full_blocks=f['class_block_queries'],
            off_solver=r['solver_calls'],full_solver=f['solver_calls']))
    asummary=dict(total_units=2520,failures=sum(r['status']!='complete' for r in rows),
        censored=sum(bool(r['censored']) for r in rows),valid_full_class_off_units=len(matched),
        wall_ratio=old.paired_ratio(matched,'off_wall','full_wall','batch'),
        block_query_ratio=old.paired_ratio(matched,'off_blocks','full_blocks','batch'),
        solver_call_ratio=old.paired_ratio(matched,'off_solver','full_solver','batch'),
        objective_off_scope='Disable SAT model extension in the active class method; retain the same encoding and other stages.')
    provenance=dict(formal_source_directory=str(formal),ablation_source_directory=str(ablation),
        source_commit=fp['source_commit'],budget_policy=fp['budget_policy'],old_table_snapshot=str(legacy_csv),
        old_table_sha256=sha(legacy_csv),script_sha256=sha(__file__),estimator_sha256=sha(old.__file__),
        independent_replay=str(replay),independent_replay_sha256=sha(replay),
        opponent_detection=str(previous_detection),opponent_detection_sha256=sha(previous_detection),
        ratio_policy='C65: average log ratios over valid seeds within each workload; 10000 workload-cluster draws, seed 0; failures/censoring/zero-call pairs disclosed.',
        note=NOTE)
    counts=dict(total_units=700,failed_units=sum(u['status']!='complete' for u in fr),
        censored_units=sum(bool(u.get('censored')) for u in fr),
        unsupported_units=sum(u.get('tier_timing_status')=='not_applicable_no_execution' for u in depths),
        measured_tiers_units=sum(u.get('tier_timing_status')=='measured' for u in depths),
        explicit_block_timeouts=sum(u.get('class_timeout',0) for u in depths),
        block_unknowns=sum(u.get('class_unknown',0) for u in depths))
    acount=dict(total_units=2520,failed_units=sum(u['status']!='complete' for u in ar),
        censored_units=sum(bool(u.get('censored')) for u in ar),
        explicit_block_timeouts=sum(u.get('class_timeout',0) for u in rows),
        block_unknowns=sum(u.get('class_unknown',0) for u in rows))
    out.mkdir(parents=True,exist_ok=True)
    old.csv_write(out/'table3-leetcode-c69.csv',sorted(lc,key=lambda r:(int(r['problem']),int(r['seed']))),fields)
    old.csv_write(out/'table-ablation-xdatabm-c69.csv',aggregate,list(aggregate[0]))
    write(out/'table3-leetcode-c69.json',dict(summary=ls,counts=counts,provenance=provenance,
        formal_depth_rows=depths,units=lc,objective=objective(fr)))
    write(out/'table-ablation-xdatabm-c69.json',dict(summary=asummary,rows=aggregate,units=rows,counts=acount,provenance=provenance))
    print(json.dumps(dict(leetcode=ls,ablation_counts=acount)))

if __name__=='__main__':
    p=argparse.ArgumentParser()
    for k in ('formal','ablation','legacy_csv','replay','out'):p.add_argument('--'+k.replace('_','-'),required=True,type=Path)
    a=p.parse_args();summarize(a.formal,a.ablation,a.legacy_csv,a.replay,a.out)
