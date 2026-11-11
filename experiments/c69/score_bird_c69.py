"""C69 receipts only; reuse the frozen BIRD sampling and scoring functions."""
import argparse, collections, csv, hashlib, json, math, random, statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import score_bird_public_original_paired_v1 as original

METHODS=('seer_stream_sqlwitness','sqlwitness','parseval')
def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def csv_write(p,rows):
    with p.open('w',newline='') as h:
        writer=csv.DictWriter(h,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)

def prefetch(items,load,width=32):
    # Bounded read-only I/O overlap; output order and all assertions are unchanged.
    with ThreadPoolExecutor(max_workers=width) as pool:
        iterator=iter(items)
        pending=collections.deque(pool.submit(load,item) for item in __import__('itertools').islice(iterator,width))
        while pending:
            value=pending.popleft().result()
            item=next(iterator,None)
            if item is not None:pending.append(pool.submit(load,item))
            yield value

def load_record(item):
    key,record=item
    if record['status']!='complete':return key,record,None
    d=Path(record['path']);cb=(d/'config.json').read_bytes();rb=(d/'result.json').read_bytes()
    return key,record,(d,json.loads(cb),json.loads(rb),hashlib.sha256(rb).hexdigest(),hashlib.sha256(cb).hexdigest())

def pair_inventory(source,ids):
    """Keep every submitted identity pair; SQL-text equality is diagnostic only."""
    rows=[];changes=[]
    for bid in ids:
        qs=source[bid]['queries'];raw=[q['sql'] for q in qs]
        normalized=[' '.join(q.split()) for q in raw]
        total=len(qs)*(len(qs)-1)//2
        unequal_raw=sum(a!=b for i,a in enumerate(raw) for b in raw[i+1:])
        unequal_normalized=sum(a!=b for i,a in enumerate(normalized) for b in normalized[i+1:])
        # Independent multiplicity identity guards against accidental deduplication.
        assert unequal_raw==total-sum(n*(n-1)//2 for n in collections.Counter(raw).values())
        rows.append(dict(batch=bid,identity_pairs=total,raw_unequal_sql_pairs=unequal_raw,
                         whitespace_normalized_unequal_sql_pairs=unequal_normalized,
                         distinct_queries=len(set(normalized))))
        for i,a in enumerate(raw):
            for j in range(i+1,len(raw)):
                if a!=raw[j] and normalized[i]==normalized[j]:
                    changes.append(dict(batch=bid,qids=[qs[i]['qid'],qs[j]['qid']],raw_sql=[a,raw[j]]))
    return dict(identity_pairs=sum(r['identity_pairs'] for r in rows),
        raw_unequal_sql_pairs=sum(r['raw_unequal_sql_pairs'] for r in rows),
        whitespace_normalized_unequal_sql_pairs=sum(r['whitespace_normalized_unequal_sql_pairs'] for r in rows),
        whitespace_only_identity_pairs=changes,batches=rows)

def paired_efficiency(sample,units,opponent):
    # Preserve the full frozen sampling frame, including failed batches. The
    # unchanged estimator is calculable only on complete five-seed pairs.
    complete=[r for r in sample if all((r['batch'],s,m) in units
              for s in range(5) for m in (METHODS[0],opponent))]
    missing=[r for r in sample if r not in complete]
    expected_weight=sum(r['design_weight'] for r in sample)
    mapped={}
    for row in complete:
        for seed in range(5):
            mapped[row['batch'],seed,'seer_stream']=units[row['batch'],seed,METHODS[0]]
            mapped[row['batch'],seed,'sqlwitness']=units[row['batch'],seed,opponent]
    answer=original.efficiency(complete,mapped) if complete else dict(wall=None,requests=None,completed_wall=None,censored_sample_batches=0)
    answer.update(expected_sample_batches=len(sample),expected_paired_units=len(sample)*5,
        available_sample_batches=len(complete),missing_sample_batches=len(missing),
        missing_batches=[r['batch'] for r in missing],expected_design_weight=expected_weight,
        zero_request_batches=[r['batch'] for r in complete if any(not units[r['batch'],seed,m]['requests'] for seed in range(5) for m in (METHODS[0],opponent))],
        available_design_weight=sum(r['design_weight'] for r in complete),
        sample_frame=[dict(**r,available_seeds=[seed for seed in range(5)
            if (r['batch'],seed,METHODS[0])in units and (r['batch'],seed,opponent)in units]) for r in sample],
        ratio_scope='Available five-seed paired batches only; full-sample ratio is missing when any required value is unavailable. No failed batch is removed from the expected denominator.',
        full_sample_wall_ratio=answer['wall']['geomean_ratio_opponent_over_seer'] if not missing and answer['wall'] else None,
        full_sample_request_ratio=answer['requests']['geomean_ratio_opponent_over_seer']
            if answer['requests'] and answer['requests']['batches']==len(sample) else None)
    faster=[r for r in complete if sum(math.log(units[r['batch'],seed,opponent]['wall_s']/units[r['batch'],seed,METHODS[0]]['wall_s']) for seed in range(5))>0]
    wins=sum(r['design_weight'] for r in faster);unknown=sum(r['design_weight'] for r in missing)
    answer['seer_faster_full_sample']=dict(known_faster_batches=len(faster),unknown_batches=len(missing),
        expected_batches=len(sample),unweighted_fraction_bounds=[len(faster)/len(sample),(len(faster)+len(missing))/len(sample)] if sample else None,
        design_weighted_fraction_bounds=[wins/expected_weight,(wins+unknown)/expected_weight] if expected_weight else None,
        point=(wins/expected_weight if expected_weight else None) if not missing else None)
    return answer

def summarize(root,out):
    source,test,dev=original.input_rows();sample=original.sample_rows(test)
    test_sha=sha(original.DATA/'test.ids');dev_sha=sha(original.DATA/'dev.ids')
    assert test_sha=='c17d7e80bc8df43b74fab0f14b6ac4c4023bc6600452af521aca28583e1e3886'
    assert dev_sha=='0bce15b902b0bd73c68c0102ebebd60e98ef84d82c7d79a4d7c1f78ea71b48d8'
    inventories={'test':pair_inventory(source,test),'dev':pair_inventory(source,dev)}
    assert inventories['test']['raw_unequal_sql_pairs']==64085
    assert inventories['dev']['raw_unequal_sql_pairs']==15193
    assert inventories['test']['identity_pairs']==len(test)*original.PAIRS
    assert inventories['dev']['identity_pairs']==len(dev)*original.PAIRS
    plan=read(root/'config.json');terminal=read(root/'result.json')
    assert all(sha(p)==h for p,h in plan['adapter_files'].items())
    assert {g['batch'] for g in plan['groups'][:200*5]}=={r['batch'] for r in sample}
    assert plan['stage']=='bird' and plan['units']==terminal['total']==1534*5*3
    assert terminal['complete'], 'wait for all attempts to reach a terminal state'
    assert len(terminal['records'])==plan['units']
    expected={(b,s,m) for b in source for s in range(5) for m in METHODS}
    records={(r['batch'],r['seed'],r['variant']):r for r in terminal['records']}
    assert set(records)==expected and len(records)==len(terminal['records'])
    group_paths=[root/'groups'/f'{i}.json' for i in range(len(plan['groups']))]
    for i,(g,observed) in enumerate(zip(plan['groups'],prefetch(group_paths,read))):
        if i%1000==0:print(json.dumps(dict(event='checking_groups',groups=i,total=len(group_paths))),flush=True)
        rs=observed['records'];assert [r['variant'] for r in rs]==g['order']
        assert len({r['core'] for r in rs})==1
        assert all(a['finished']<=b['started'] for a,b in zip(rs,rs[1:]))
    print(json.dumps(dict(event='validated_group_roster',groups=len(plan['groups']),units=plan['units'])),flush=True)
    units={};details=[];failures=[]
    for serial,(key,record,loaded) in enumerate(prefetch(sorted(records.items()),load_record),1):
        if serial%2000==0:print(json.dumps(dict(event='reading_receipts',units=serial,total=len(records))),flush=True)
        bid,seed,method=key
        if record['status']!='complete':failures.append(record);continue
        d,c,r,result_hash,config_hash=loaded;m=r['metrics']
        assert result_hash==record['result_sha256']
        assert r['config_sha256']==config_hash
        assert c['source_commit']==plan['source_commit']
        assert (c['batch'],c['seed'],c['method'])==key
        assert c['budget_s']==600 and c['order']=='shuffle' and c['method_seed']==0
        assert c['affinity']==[record['core']]
        ids=[q['qid'] for q in source[bid]['queries']];assert c['query_ids']==ids
        arrival=ids[:];random.Random(seed).shuffle(arrival)
        observed=[e['query'] for e in r['events'] if e.get('event')=='arrival']
        assert observed==arrival[:m['processed_queries']]
        assert m['processed_queries']+m['unprocessed_queries']==11
        assert r['unprocessed_query_ids']==arrival[m['processed_queries']:]
        assert m['censored_at_budget']==(not m['completed_within_budget'])
        assert sum(r['pair_counts'].values())==m['processed_queries']*(m['processed_queries']-1)//2
        mask=original.pair_mask(r,ids)
        if method=='sqlwitness':
            assert c['native_mode']=='official_experiment_driver_one_process_per_batch'
        elif method=='parseval':
            assert c['official_upstream_commit']=='0d58127c5d6df74caca170b80c80f312b95cc238'
            assert c['native_generation_seed']==12322 and c['native_pair_timeout_s']==360
        else:assert c['stream_schedule']=='class_bounded' and not c['single_bucket_target']
        if method!=METHODS[0]:
            assert r['pair_counts']['proved']==r['pair_counts']['bounded']==0
            assert m['pair_decision_requests']==m['native_pair_calls']
        v=dict(batch=bid,seed=seed,method=method,split='test' if bid in test else 'dev',
            mask=mask,separated=mask.bit_count(),proved=r['pair_counts']['proved'],
            bounded=r['pair_counts']['bounded'],undecided=r['pair_counts']['undecided'],
            processed=m['processed_queries'],wall_s=m['wall_s'],requests=m['pair_decision_requests'],
            censored=bool(m['censored_at_budget']),invalid_certificates=m['invalid_admitted_certificates'],
            native_errors=m.get('native_errors',sum(c.get('native',{}).get('verdict') in ('error','syntax_error') for c in r.get('calls',[]))),
            native_timeouts=m.get('native_timeouts',sum(c.get('native',{}).get('verdict')=='timeout' for c in r.get('calls',[]))),
            objective_rounds=m.get('objective_rounds',0),objective_extra_separations=m.get('objective_extra_separations',0),
            objective_candidates=m.get('objective_candidates',0),source_result=str(d/'result.json'),
            interrupted_class_receipt=any(e.get('event')=='class_bounded_search' and e['receipt'].get('reason') in
                ('class_hard_timeout','class_worker_exited') for e in r.get('events',[])),
            result_sha256=record['result_sha256'],config_sha256=r['config_sha256'])
        assert math.isfinite(v['wall_s']) and v['wall_s']>0
        units[key]=v;details.append(v)
    # Only the figure-size covariate collapses SQL whitespace. Detection uses
    # original.pair_mask over all submitted identities, exactly as the frozen scorer.
    sizes={r['batch']:r['distinct_queries'] for v in inventories.values() for r in v['batches']}
    efficiency={op:paired_efficiency(sample,units,op) for op in METHODS[1:]}
    size_bins={op:[] for op in METHODS[1:]};points=[]
    for op in METHODS[1:]:
        for low,high in ((1,3),(4,6),(7,9),(10,11)):
            selected=[r for r in sample if low<=sizes[r['batch']]<=high]
            size_bins[op].append(dict(min_distinct_queries=low,max_distinct_queries=high,
                **paired_efficiency(selected,units,op)))
        for row in sample:
            b=row['batch'];pairs=[(units[b,s,METHODS[0]],units[b,s,op]) for s in range(5)
                                 if (b,s,METHODS[0])in units and (b,s,op)in units]
            points.append(dict(batch=b,opponent=op,distinct_queries=sizes[b],expected_seeds=5,available_seeds=len(pairs),
                stratum=row['stratum'],design_weight=row['design_weight'],
                wall_ratio=math.exp(statistics.mean(math.log(y['wall_s']/x['wall_s']) for x,y in pairs)) if len(pairs)==5 else None,
                request_ratio=(math.exp(statistics.mean(math.log(y['requests']/x['requests']) for x,y in pairs))
                               if len(pairs)==5 and all(x['requests'] and y['requests'] for x,y in pairs) else None),
                censored=any(x['censored'] or y['censored'] for x,y in pairs) if len(pairs)==5 else None))
    table=[];totals={}
    for method in METHODS:
        rs=[v for v in units.values() if v['method']==method and v['split']=='test']
        per_seed=[dict(seed=s,completed_units=sum(v['seed']==s for v in rs),
            expected_units=len(test),
            failed_units=sum(r['batch'] in test and r['variant']==method and r['seed']==s for r in failures),
            expected_identity_pairs=inventories['test']['identity_pairs'],
            certified_separated_pairs=sum(v['separated'] for v in rs if v['seed']==s)) for s in range(5)]
        assert all(x['completed_units']+x['failed_units']==x['expected_units'] for x in per_seed)
        failed_test=sum(x['failed_units'] for x in per_seed)
        observed_pairs=sum(v['processed']*(v['processed']-1)//2 for v in rs)
        unprocessed_pairs=sum(original.PAIRS-v['processed']*(v['processed']-1)//2 for v in rs)
        assert observed_pairs+unprocessed_pairs+failed_test*original.PAIRS==inventories['test']['identity_pairs']*5
        counts=[v['certified_separated_pairs'] for v in per_seed]
        e=efficiency.get(method,{})
        row=dict(method=method,expected_test_units=len(test)*5,completed_test_units=len(rs),
            failed_test_units=sum(r['batch'] in test and r['variant']==method for r in failures),
            missing_terminal_test_units=0,expected_test_identity_pairs_per_seed=inventories['test']['identity_pairs'],
            expected_test_identity_pairs_all_seeds=inventories['test']['identity_pairs']*5,
            observed_processed_identity_pairs=observed_pairs,unprocessed_identity_pairs=unprocessed_pairs,
            failed_unit_identity_pairs=failed_test*original.PAIRS,
            efficiency_expected_batches=200 if e else None,
            efficiency_available_batches=e.get('available_sample_batches'),
            efficiency_missing_batches=e.get('missing_sample_batches'),
            efficiency_ratio_scope=e.get('ratio_scope'),
            wall_ratio_available_batches=e['wall']['batches'] if e.get('wall') else 0,
            request_ratio_available_batches=e['requests']['batches'] if e.get('requests') else 0,
            request_zero_batches=len(e.get('zero_request_batches',[])) if e else None,
            full_sample_wall_ratio_opponent_over_seer=e.get('full_sample_wall_ratio'),
            full_sample_request_ratio_opponent_over_seer=e.get('full_sample_request_ratio'),
            censored_test_units=sum(v['censored'] for v in rs),
            certified_separated_seed0=counts[0],certified_separated_mean=statistics.mean(counts),
            certified_separated_sample_sd=statistics.stdev(counts),
            invalid_certificates=sum(v['invalid_certificates'] for v in rs),
            native_errors=sum(v['native_errors'] for v in rs),native_timeouts=sum(v['native_timeouts'] for v in rs),
            wall_ratio_opponent_over_seer=e.get('wall',{}).get('geomean_ratio_opponent_over_seer') if e.get('wall') else None,
            wall_ci95=json.dumps(e.get('wall',{}).get('ci95')) if e.get('wall') else None,
            seer_faster_fraction=e.get('seer_faster_full_sample',{}).get('point'),
            seer_faster_fraction_bounds=json.dumps(e.get('seer_faster_full_sample',{}).get('design_weighted_fraction_bounds')) if e else None,
            seer_faster_observed_fraction=e.get('wall',{}).get('seer_better_batch_fraction') if e.get('wall') else None,
            request_ratio_opponent_over_seer=e.get('requests',{}).get('geomean_ratio_opponent_over_seer') if e.get('requests') else None,
            request_ci95=json.dumps(e.get('requests',{}).get('ci95')) if e.get('requests') else None)
        totals[method]=dict(**row,per_seed=per_seed);table.append(row)
    candidates=sum(v['objective_candidates'] for v in units.values() if v['method']==METHODS[0])
    extra=sum(v['objective_extra_separations'] for v in units.values() if v['method']==METHODS[0])
    assert len(table)==3 and len(points)==200*2
    assert all(sum(x['expected_sample_batches'] for x in size_bins[op])==200 for op in METHODS[1:])
    output=dict(source_directory=str(root),source_commit=plan['source_commit'],expected_units=plan['units'],
        completed_units=len(units),failed_units=len(failures),failures=failures,
        input_sha256=original.INPUT_SHA,sample_sha256=original.SAMPLE_SHA,
        test_ids_sha256=test_sha,dev_ids_sha256=dev_sha,
        test_batches=len(test),dev_batches=len(dev),missing_terminal_units=0,
        test_identity_pairs_per_seed=inventories['test']['identity_pairs'],
        test_raw_unequal_sql_identity_pairs_per_seed=inventories['test']['raw_unequal_sql_pairs'],
        denominator_audit=dict(**inventories,
            cause='The 64085/15193 constants count unequal raw SQL strings. The failed expression first collapsed whitespace, yielding 64080/15191. Frozen input and split hashes match; no missing run causes this arithmetic difference.',
            denominator='All 11 choose 2 submitted identity pairs per batch, including duplicate SQL, failed units and unprocessed queries. The unequal-SQL counts are diagnostic only.',
            size_covariate='Distinct whitespace-normalized SQL strings, case preserved; only used for figure bins.'),
        test_method_totals=totals,paired_efficiency_test_200=efficiency,size_bins=size_bins,
        objective=dict(candidates=candidates,extra_separations=extra,extra_per_candidate=extra/candidates if candidates else None,
            units_with_interrupted_class_receipts=sum(v['interrupted_class_receipt'] for v in units.values() if v['method']==METHODS[0]),
            scope='Returned class receipts; interrupted detailed component counts may be incomplete, while entry solver counts remain authoritative.'),
        certification='Original common admission: Sigma validation and DBMS replay; no invented independent replay evidence.',
        scoring='Original score_bird_public_original_paired_v1 input_rows, sample_rows, pair_mask and efficiency; opponent mapped separately, five seeds clustered within frozen strata.',
        missing='Every failed unit remains in the expected test-unit and identity-pair denominators. Observed detection is a lower bound with failed units unknown. Efficiency reports available-case estimates, retains the full 200-batch frame, and leaves full-sample ratios null when a required receipt is missing.',
        terminal_result_sha256=sha(root/'result.json'),plan_sha256=sha(root/'config.json'),
        resource_policy=read(root.parent/'resource-policy-v2.json') if (root.parent/'resource-policy-v2.json').exists() else None,
        receipts=details,script_sha256=sha(__file__),original_scoring_sha256=sha(original.__file__))
    out.mkdir(parents=True,exist_ok=True)
    csv_write(out/'table-birdpool-c69.csv',table)
    if points:csv_write(out/'birdpool-c69-size-points.csv',points)
    (out/'table-birdpool-c69.json').write_text(json.dumps(output,indent=2)+'\n')
    print(json.dumps(dict(completed_units=len(units),failed_units=len(failures),totals=totals)))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('out',type=Path)
    a=p.parse_args();summarize(a.root,a.out)
