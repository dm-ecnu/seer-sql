"""Preserve published candidate identities; no labels, SQL synthesis or solver calls."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import collections
import csv
import hashlib
import json
from pathlib import Path

RAW=Path((SEER_ROOT + '/datasets/raw/public-batch-v1'))
BASE=RAW.parents[2]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def unique_object(pairs):
    result={}
    for key,value in pairs:
        assert key not in result,('duplicate JSON key',key)
        result[key]=value
    return result

def read_json(path):
    return json.loads(path.read_text(),object_pairs_hook=unique_object)

def publish(path,rows):
    content=''.join(json.dumps(r,sort_keys=True,ensure_ascii=False)+'\n' for r in rows)
    if path.exists():
        assert path.read_text()==content,('existing input differs',str(path))
    else:
        tmp=path.with_suffix('.tmp')
        with tmp.open('x') as out: out.write(content)
        tmp.replace(path)
    return {'path':str(path),'rows':len(rows),'sha256':sha(path)}

def main():
    entries=read_json(RAW/'source-files.json')
    sources={}
    for entry in entries:
        path=RAW/entry['dest'];data=path.read_bytes()
        blob=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        assert blob==entry['sha'],entry['dest']
        sources[entry['dest']]={**entry,'sha256':sha(path),'bytes':len(data)}
    assert len(sources)==27
    gold=read_json(RAW/'spotit/dev.json')
    assert len(gold)==1534
    assert [r['question_id'] for r in gold]==list(range(1534))
    models={p.stem:read_json(p) for p in sorted((RAW/'spotit').glob('*_sql.json'))}
    assert len(models)==10
    for name,rows in models.items():
        assert set(rows)=={str(i) for i in range(1534)},name
        assert all(isinstance(x,str) for x in rows.values()),name
    existing=[json.loads(l) for l in (BASE/'datasets/batches/bird_pairs.jsonl').read_text().splitlines()]
    schemas={};old_gold=collections.defaultdict(set)
    for batch in existing:
        db=batch['meta']['db_id'];path=(BASE/'datasets/batches'/batch['schema_path']).resolve()
        assert db not in schemas or schemas[db]['path']==str(path)
        schemas[db]={'path':str(path),'sha256':sha(path)}
        old_gold[db,batch['queries'][0]['sql'].strip().removesuffix(';')].add(batch['batch_id'])
    assert len(schemas)==11
    bird=[];sizes=collections.Counter();suffixes=collections.Counter();exact_old=0
    for row in gold:
        i=row['question_id'];db=row['db_id'];assert db in schemas
        candidates=[{'qid':'gold','sql':row['SQL'],'origin_kind':'released_gold','source_file':'spotit/dev.json','source_key':i}]
        for name,preds in models.items():
            raw=preds[str(i)];sql,marker,suffix=raw.partition('\t----- bird -----\t')
            if marker:
                assert suffix.strip()==db,(name,i,suffix,db)
                suffixes[name]+=1
            else:
                sql=raw
            candidates.append({'qid':name,'sql':sql,'source_raw':raw,'source_file':'spotit/'+name+'.json','source_key':str(i),'metadata_suffix_removed':bool(marker)})
        assert len(candidates)==11 and len({q['qid'] for q in candidates})==11
        sizes[len({q['sql'].strip() for q in candidates})]+=1
        old=sorted(old_gold.get((db,row['SQL'].strip().removesuffix(';')),set()))
        exact_old+=bool(old)
        bird.append({'batch_id':'spotit-bird-'+str(i),'db_id':db,'question_id':i,'dialect':'sqlite','queries':candidates,'declared_schema':schemas[db],'existing_gold_text_matches':old,'evaluation_status':'source_prepared_not_protocol_frozen'})
    result={'verified_source_files':len(sources),'sources':sources,'bird':{**publish(RAW/'bird-published-pools.jsonl',bird),'queries':sum(len(r['queries']) for r in bird),'distinct_sql_per_batch_histogram':dict(sorted(sizes.items())),'metadata_suffixes':dict(suffixes),'exact_existing_gold_matches':exact_old,'schema_basis':'existing frozen declared schemas; mined constraints not silently substituted'},'rewrites':{}}
    for family in ('tpch','dsb','job_syn'):
        grouped=collections.defaultdict(list);raw_rows=0;missing=[]
        for path in sorted((RAW/'llmr2').glob('*pool_'+family+'_cleaned.csv')):
            for index,row in enumerate(csv.DictReader(path.open(encoding='utf-8-sig',newline=''))):
                raw_rows+=1
                if not row['original_sql'] or not row['rewritten_sql']:
                    missing.append({'source_file':'llmr2/'+path.name,'source_row':index,'record':row})
                grouped[row['db_id'],row['original_sql']].append({'source_file':'llmr2/'+path.name,'source_row':index,'record':row})
        tests=list(csv.DictReader((RAW/'llmr2'/('queries_'+family+'_test.csv')).open(encoding='utf-8-sig',newline='')))
        testkeys={(r['db_id'],r['original_sql']) for r in tests}
        batches=[];hist=collections.Counter()
        for (db,original),members in sorted(grouped.items()):
            identity=hashlib.sha256(json.dumps([db,original],ensure_ascii=False).encode()).hexdigest()
            queries=[{'qid':'original','sql':original,'origin_kind':'original'}]
            for member in members:
                queries.append({'qid':Path(member['source_file']).stem+'-r'+str(member['source_row']),'sql':member['record']['rewritten_sql'],'source_file':member['source_file'],'source_row':member['source_row'],'source_record':member['record'],'source_status':'present' if member['record']['rewritten_sql'] else 'missing_sql'})
            assert len({q['qid'] for q in queries})==len(queries)
            hist[len({q['sql'].strip() for q in queries if q['sql']})]+=1
            batches.append({'batch_id':'llmr2-'+family+'-'+identity,'db_id':db,'dialect':'postgres','queries':queries,'exact_original_in_author_test':(db,original) in testkeys,'author_schema_file':'llmr2/'+('job' if family=='job_syn' else family)+'.json','evaluation_status':'source_prepared_schema_contract_and_protocol_pending','equivalence_labels':'not inferred from positive/negative training pool names'})
        assert sum(len(r['queries'])-1 for r in batches)==raw_rows
        assert len({r['batch_id'] for r in batches})==len(batches)
        result['rewrites'][family]={**publish(RAW/('llmr2-'+family+'-pools.jsonl'),batches),'source_rows':raw_rows,'missing_sql_rows':missing,'distinct_sql_per_batch_histogram':dict(sorted(hist.items())),'exact_originals_in_author_test':sum(r['exact_original_in_author_test'] for r in batches)}
    publish(RAW/'prepared-summary.jsonl',[result])
    print(json.dumps({key:({k:v for k,v in value.items() if k!='missing_sql_rows'} if isinstance(value,dict) else value) for key,value in result.items() if key!='sources'},sort_keys=True))

if __name__=='__main__': main()
