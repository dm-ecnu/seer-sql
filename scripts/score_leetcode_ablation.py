"""Publish only a complete, independently replayed encoding ablation cohort.

DBMS replay is performed by the frozen per-unit checker. This reader rebinds
every raw/checked/done hash, validates the graph population, and computes pooled
pair coverage without retaining all 54 million pair objects in memory.
"""
import argparse,collections,csv,hashlib,io,itertools,json,math,os,statistics,subprocess,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT.parent
FROZEN=ROOT
OUT=BASE/'leetcode-ablation-scale-v1'
DATA=BASE/'datasets/batches/leetcode-scale-v1/leetcode_scale.jsonl'
sys.path.insert(0,str(FROZEN/'scripts'))

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def readiness(protocol,out):
    missing=[];paths=[]
    for bid in protocol['test_ids']:
        for method in protocol['methods']:
            for seed in protocol['seeds']:
                path=out/method/('s'+str(seed))/bid
                paths.append((bid,method,seed,path))
                if not (path/'done.json').is_file():missing.append(str(path))
    assert len(paths)==protocol['expected_test_units']
    return paths,missing

def pack_graph(queries,row):
    ids=[q['qid'] for q in queries];n=len(ids);edges=n*(n-1)//2
    assert len(set(ids))==n and len(row['pairs'])==edges
    errors=set(row['error_class']);assert errors<=set(ids)
    bits=bytearray((edges+7)//8);counts=collections.Counter();gains=[0]*len(row['dbs']);times={}
    for index,((a,b),pair) in enumerate(zip(itertools.combinations(ids,2),row['pairs'])):
        assert (pair['a'],pair['b'])==(a,b),'pair graph order/population changed'
        cert=pair['certificate']
        assert pair['verdict']==('split' if cert is not None else 'undecided')
        if a in errors or b in errors:
            assert cert is None;counts['query_error']+=1
        elif cert is None:counts['undecided']+=1
        else:
            assert type(cert) is int and 0<=cert<len(gains)
            accepted=pair['accepted_s'];assert 0<=accepted<row['budget']
            first,last=times.get(cert,(accepted,accepted))
            times[cert]=(min(first,accepted),max(last,accepted))
            bits[index//8]|=1<<(index%8);gains[cert]+=1;counts['split']+=1
    mask=int.from_bytes(bits,'little')
    assert mask.bit_count()==row['n_genuine_pairs']==counts['split']
    assert row['last_accept_s']==max((last for first,last in times.values()),default=None)
    # Database IDs are in parent receipt order; first witnesses are disjoint.
    ordered=[times[i] for i in sorted(times)]
    assert all(a[1]<=b[0] for a,b in zip(ordered,ordered[1:]))
    cumulative=[0]
    for gain in gains:cumulative.append(cumulative[-1]+gain)
    return mask,dict(counts),cumulative

def read_verified_unit(path,stamp):
    marker=json.loads((path/'done.json').read_text())
    assert marker['protocol_sha256']==stamp
    assert marker['raw_sha256']==sha(path/'raw.jsonl') and marker['checked_sha256']==sha(path/'checked.json')
    checked=json.loads((path/'checked.json').read_text())
    assert checked['raw_sha256']==marker['raw_sha256']
    row=json.loads((path/'raw.jsonl').read_text())
    return marker,checked,row

def native_returns(entries,in_window=True):
    return dict(collections.Counter(e['verdict'] for e in entries
        if e['kind']=='generation_api_finished' and (not in_window or e['within_acceptance_window'])))

def load_unit(path,protocol,stamp,batch,method,seed):
    marker,checked,row=read_verified_unit(path,stamp)
    from leetcode_ablation_protocol import verify_configuration
    runtime,pre=verify_configuration(row,method,seed,protocol['runtime'])
    assert marker['worker_runtime_sha256']==sha(Path(row['journal'])/'worker-runtime.json')
    variant=method;method='pair_seer'
    bid=batch['batch_id'];n=len(batch['queries'])
    assert (row['batch_id'],row['tool'],row['seed'],row['budget'])==(bid,method,seed,60)
    assert (checked['batch_id'],checked['method'],checked['seed'],checked['n'],checked['split'])==(bid,method,seed,n,'test')
    assert not row.get('runner_error') and not row.get('worker_error')
    assert row['source_status']==checked['source_status'] and row['source_status'] in ('completed','deadline')
    contract=row['comparison_contract']
    assert (contract['tool'],contract['seed'],contract['budget_s'])==(method,seed,60)
    assert contract['hard_parent_receipt_deadline'] and contract['fresh_common_dbms_runs']==2
    assert contract['files'] and all(protocol['files'].get(p)==h for p,h in contract['files'].items())
    assert row['service_cleanup']['verified'] and all(x['verified'] for x in row['external_backend_cleanup'])
    assert row['cpu_host_complete'] and not row['cpu_complete']
    assert not row['call_metrics']['partial_line']
    assert checked['entry_counts']==row['call_metrics']['counts']
    for a,b in (('wall_secs','wall_secs'),('cpu_secs','host_cpu_secs'),('service_peak_memory_bytes','service_peak_memory_bytes')):
        assert row[a]==checked[b] and math.isfinite(row[a]) and row[a]>0
    mask,counts,cumulative=pack_graph(batch['queries'],row)
    assert counts==checked['independent_outcomes']
    assert checked['accepted_databases']==len(row['dbs'])
    metrics=row['call_metrics']['counts'];entries=row['call_metrics']['entries']
    assert native_returns(entries,in_window=False)==checked['native_verdicts']
    native=native_returns(entries)
    item={'batch_id':bid,'parent_batch_id':batch['meta']['parent_batch_id'],'method':variant,'seed':seed,'n':n,
        'pairs':n*(n-1)//2,'split_pairs':counts.get('split',0),'query_error_pairs':counts.get('query_error',0),
        'undecided_pairs':counts.get('undecided',0),'source_deadline':int(row['source_status']=='deadline'),
        'receiver_wall_s':row['wall_secs'],'host_cpu_s':row['cpu_secs'],
        'service_memory_mib_through_graph':row['service_peak_memory_bytes']/1024**2,
        'accepted_databases':len(row['dbs']),
        'generation_api_starts':None if method=='pair_seer' else metrics.get('generation_api_start',0),
        'python_solver_check_starts':metrics.get('python_solver_check_attempt',0) if method in ('pair_seer','parseval') else None,
        'source_query_execute_starts':None if method=='xdatan' else metrics.get('source_query_execute_attempt',0),
        'common_query_execute_starts':metrics.get('common_query_execute_attempt',0),
        'native_completed_returns':None if method=='pair_seer' else sum(native.values()),
        'native_unsupported_returns':None if method=='pair_seer' else native.get('unsupported',0),
        'native_error_returns':None if method=='pair_seer' else native.get('error',0),
        'native_timeout_returns':None if method=='pair_seer' else native.get('timeout',0),
        'raw_sha256':marker['raw_sha256'],'checked_sha256':marker['checked_sha256']}
    item.update(mechanism_metrics(row,pre))
    return item,mask,cumulative,marker

def mechanism_metrics(row,pre):
    entries=[e for e in row['call_metrics']['entries'] if e['within_acceptance_window']]
    built=[e for e in entries if e['kind']=='encoding_level_built']
    starts=[e for e in entries if e['kind']=='encoding_level_start']
    # Completed encoding durations are a lower bound if cutoff interrupted a level.
    return {'pre_symbolic_sha256':pre[0]['sha256'] if pre else None,
            'pre_symbolic_pending_pairs':pre[0]['pending_pairs'] if pre else None,
            'pre_symbolic_accepted_databases':pre[0]['accepted_databases'] if pre else None,
            'pre_symbolic_elapsed_s':pre[0]['elapsed_s'] if pre else None,
            'encoding_levels_started':len(starts),'encoding_levels_completed':len(built),
            'encoding_seconds_completed_lower_bound':sum(e['encode_s'] for e in built),
            'encoding_memo_miss_completed':sum(e['encoding_stats'].get('memo_miss',0) for e in built),
            'encoding_memo_discarded_completed':sum(e['encoding_stats'].get('cross_query_memo_entries_discarded',0) for e in built),
            'sigma_rejections_observed':sum(e['kind']=='symbolic_sigma_rejected' for e in entries)}


def prefix_coverage(curves,k):
    assert k>=0 and curves and all(c and c[0]==0 for c in curves)
    return sum(c[min(k,len(c)-1)] for c in curves)

def mean_sd(values):
    available=[v for v in values if v is not None]
    assert all(math.isfinite(v) for v in available)
    if len(available)!=len(values):return {'mean':None,'sample_sd':None,'repeats_observed':len(available)}
    return {'mean':statistics.mean(available),'sample_sd':statistics.stdev(available) if len(available)>1 else None,'repeats_observed':len(available)}

def csv_text(rows):
    assert rows
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(rows[0]),lineterminator='\n')
    writer.writeheader();writer.writerows(rows);return stream.getvalue()

def prelude_comparisons(rows,protocol):
    groups=collections.defaultdict(list)
    for row in rows:groups[row['batch_id'],row['seed']].append(row)
    result=[]
    for (bid,seed),items in sorted(groups.items()):
        assert {r['method'] for r in items}==set(protocol['methods'])
        hashes={r['pre_symbolic_sha256'] for r in items}
        result.append({'batch_id':bid,'seed':seed,'n':items[0]['n'],
            'all_variants_have_pre_symbolic_snapshot':all(r['pre_symbolic_sha256'] is not None for r in items),
            'same_pre_symbolic_state':len(hashes)==1 and None not in hashes,
            'state_hashes':json.dumps({r['method']:r['pre_symbolic_sha256'] for r in items},sort_keys=True)})
    return result


def aggregate(protocol,stamp):
    source=[json.loads(l) for l in DATA.read_text().splitlines()]
    batches={r['batch_id']:r for r in source if r['batch_id'] in protocol['test_ids']}
    assert set(batches)==set(protocol['test_ids'])
    assert all(b['meta']['split']=='test' and len(b['queries'])==b['meta']['n'] for b in batches.values())
    parents=collections.defaultdict(set)
    for b in batches.values():parents[b['meta']['n']].add(b['meta']['parent_batch_id'])
    assert set(parents)==set(protocol['sizes']) and all(len(p)==18 for p in parents.values())
    assert all(p==next(iter(parents.values())) for p in parents.values())
    rows=[];curves=collections.defaultdict(list);known=collections.Counter();proof=hashlib.sha256()
    for bid in protocol['test_ids']:
        batch=batches[bid];per_batch=[];union=0
        for method in protocol['methods']:
            for seed in protocol['seeds']:
                path=OUT/method/('s'+str(seed))/bid
                item,mask,cumulative,marker=load_unit(path,protocol,stamp,batch,method,seed)
                proof.update(json.dumps([bid,method,seed,marker],sort_keys=True).encode())
                per_batch.append((item,mask));union|=mask
                curves[method,seed,item['n']].append(cumulative)
        denominator=union.bit_count();known[len(batch['queries'])]+=denominator
        for item,mask in per_batch:
            assert (mask&union).bit_count()==item['split_pairs']
            item['known_neq_batch']=denominator
            item['known_neq_recall']=item['split_pairs']/denominator if denominator else None
            rows.append(item)
        print(json.dumps({'state':'scoring_complete_batch','batch_id':bid,'units':len(rows)}),flush=True)
    assert len(rows)==protocol['expected_test_units']
    groups=collections.defaultdict(list)
    for row in rows:groups[row['method'],row['seed'],row['n']].append(row)
    mean_columns=['receiver_wall_s','host_cpu_s','service_memory_mib_through_graph','accepted_databases',
        'generation_api_starts','python_solver_check_starts','source_query_execute_starts','common_query_execute_starts',
        'native_completed_returns','native_unsupported_returns','native_error_returns','native_timeout_returns',
        'encoding_levels_started','encoding_levels_completed','encoding_seconds_completed_lower_bound',
        'encoding_memo_miss_completed','encoding_memo_discarded_completed','sigma_rejections_observed']
    seeds=[]
    for (method,seed,n),batch_rows in sorted(groups.items()):
        assert len(batch_rows)==18 and len({r['parent_batch_id'] for r in batch_rows})==18
        pairs=sum(r['pairs'] for r in batch_rows);splits=sum(r['split_pairs'] for r in batch_rows)
        denominator=known[n];wall=sum(r['receiver_wall_s'] for r in batch_rows)
        line={'method':method,'seed':seed,'n':n,'batches':18,'pairs':pairs,'known_neq':denominator,'split_pairs':splits,
            'query_error_pairs':sum(r['query_error_pairs'] for r in batch_rows),
            'undecided_pairs':sum(r['undecided_pairs'] for r in batch_rows),
            'known_neq_recall':splits/denominator if denominator else None,
            'all_pair_split_fraction':splits/pairs,'receiver_queries_per_s':18*n/wall,
            'source_deadline_fraction':sum(r['source_deadline'] for r in batch_rows)/18}
        for key in mean_columns:
            vals=[r[key] for r in batch_rows]
            line['mean_'+key]=statistics.mean(vals) if all(v is not None for v in vals) else None
        seeds.append(line)
    assert len(seeds)==len(protocol['methods'])*len(protocol['seeds'])*len(protocol['sizes'])
    summary=[]
    for method in protocol['methods']:
        for n in protocol['sizes']:
            repeated=[r for r in seeds if r['method']==method and r['n']==n]
            assert {r['seed'] for r in repeated}==set(range(5))
            for metric in repeated[0]:
                if metric in ('method','seed','n','batches','pairs','known_neq'):continue
                summary.append({'method':method,'n':n,'metric':metric,**mean_sd([r[metric] for r in repeated])})
    coverage=[]
    for n in protocol['sizes']:
        maximum=max(len(c)-1 for (m,s,size),cs in curves.items() if size==n for c in cs)
        for method in protocol['methods']:
            for k in range(maximum+1):
                covered=[prefix_coverage(curves[method,seed,n],k) for seed in protocol['seeds']]
                vals=[v/known[n] if known[n] else None for v in covered]
                coverage.append({'method':method,'n':n,'accepted_databases_per_batch_prefix':k,'known_neq':known[n],
                    **mean_sd(vals)})
    metadata={'state':'complete_all_units_independently_replayed','units':protocol['expected_test_units'],'parents_per_n':18,'seeds':list(range(5)),
        'protocol_sha256':stamp,'scorer_sha256':sha(__file__),'unit_proofs_sha256':proof.hexdigest(),
        'known_neq_by_n':dict(known),'summary':summary,
        'definitions':{'coverage':'same parent/n union of certified split pairs over three ablation configurations and five repeats; not an equivalence oracle',
            'repeats':'pool same 18 parents within each seed; mean and sample SD over five seeds',
            'wall':'receiver scope through cleanup and graph aggregation; excludes final JSON serialization',
            'memory':'single service sampled peak through graph aggregation, including startup and page cache; excludes final JSON and external DBMS',
            'host_cpu':'exclusive service CPU through graph aggregation; external MySQL/PostgreSQL excluded; total CPU unknown',
            'counters':'observed API starts and native return records inside B; failed starts included; not completed work; Java internals unknown',
            'coverage_curve':'per batch first k accepted databases; zero-database batches retained, saturated after last database',
            'stage_attribution':'completed encoding time is a lower bound when interrupted; prelude-comparisons retains every matched/unmatched state; unmatched prefixes are not a single-stage causal comparison',
            'missing_metrics':'SQLWitness solver not used; XDataN internal solver/query counts unknown; SEER generation API differs from native pair API'}}
    return {'scores.json':json.dumps(metadata,sort_keys=True,indent=2)+'\n','per-batch.csv':csv_text(rows),
            'per-seed.csv':csv_text(seeds),'summary.csv':csv_text(summary),'coverage.csv':csv_text(coverage),
            'prelude-comparisons.csv':csv_text(prelude_comparisons(rows,protocol))}

def main():
    os.environ.setdefault('XDG_RUNTIME_DIR','/run/user/'+str(os.getuid()))
    ap=argparse.ArgumentParser();ap.add_argument('--stop-timer');a=ap.parse_args()
    from leetcode_ablation_protocol import check
    protocol=check();assert protocol['version']=='leetcode-ablation-scale-v1'
    stamp=sha(OUT/'protocol.json');paths,missing=readiness(protocol,OUT)
    if missing:
        services={}
        for method in ('0','1','2'):
            unit='seer-lcablation-matrix-v1-lane'+method+'.service'
            result=subprocess.run(['systemctl','--user','show',unit,'-p','ActiveState','-p','MainPID'],capture_output=True,text=True)
            services[method]=dict(l.split('=',1) for l in result.stdout.splitlines() if '=' in l)
        print(json.dumps({'state':'waiting_no_scores_published','expected':len(paths),'complete_units':len(paths)-len(missing),
            'missing_units':len(missing),'source_services':services},sort_keys=True),flush=True)
        return 2 if any(v.get('ActiveState')=='failed' for v in services.values()) else 0
    files=aggregate(protocol,stamp)
    assert check()==protocol and sha(OUT/'protocol.json')==stamp
    destination=OUT/'scores'
    if destination.exists():
        assert set(p.name for p in destination.iterdir())==set(files)
        assert all((destination/name).read_text()==data for name,data in files.items()),'refuse changed published scores'
    else:
        temp=OUT/('.scores-'+str(os.getpid()));temp.mkdir(exist_ok=False)
        for name,data in files.items():(temp/name).write_text(data)
        temp.rename(destination)
    print(json.dumps({'state':'published_complete_scores','path':str(destination),'scores_sha256':sha(destination/'scores.json')}),flush=True)
    if a.stop_timer:
        assert a.stop_timer=='seer-lcablation-score-v1.timer'
        subprocess.run(['systemctl','--user','stop',a.stop_timer],check=True)
    return 0
if __name__=='__main__':raise SystemExit(main())
