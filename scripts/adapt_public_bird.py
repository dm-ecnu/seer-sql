"""Adapt the complete published BIRD pool without moving old development examples."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import collections
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'eval'),str(ROOT/'src')]
from matrix_score import is_dev
from pair_matrix_score import dataset,scored_pairs
from seereval.schema import Schema

BASE=Path((SEER_ROOT))
RAW=BASE/'datasets/raw/public-batch-v1'
OUT=BASE/'datasets/batches/public-batch-v1'
EXPECTED_POOL_SHA='b601d39d1641a2b50ab4415bf223ea4807b152032dcb3bf6b6c4add48c8f8eb2'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def put(path,content):
    if path.exists():assert path.read_text()==content,('refuse changed input',str(path))
    else:
        tmp=path.with_suffix(path.suffix+'.tmp')
        with tmp.open('x') as f:f.write(content)
        tmp.replace(path)

def main():
    source=RAW/'bird-published-pools.jsonl'
    assert sha(source)==EXPECTED_POOL_SHA
    rows=[json.loads(l) for l in source.read_text().splitlines()]
    assert len(rows)==1534 and [r['question_id'] for r in rows]==list(range(1534))
    frozen=BASE/'datasets/batches/bird_pairs.jsonl'
    old={r['batch_id']:r for r in map(json.loads,frozen.read_text().splitlines())}
    assert len(old)==1515
    by_question={}
    for bid,previous in old.items():
        question_id=previous['meta']['sqlwitness_index']-1
        assert question_id not in by_question and 0<=question_id<len(rows)
        source_row=rows[question_id]
        assert source_row['db_id']==previous['meta']['db_id']
        assert source_row['queries'][0]['sql'].strip().removesuffix(';')==previous['queries'][0]['sql'].strip().removesuffix(';')
        assert bid in source_row['existing_gold_text_matches']
        by_question[question_id]=bid
    wanted={'dev':set(),'test':set()}
    for bid in old:wanted['dev' if is_dev('bird_pairs',bid) else 'test'].add(bid)
    assert [len(wanted[x]) for x in ('dev','test')]==[289,1226]
    split_ids={'dev':[],'test':[]};seen=set();schemas={};batches=[];inherited=collections.Counter();new=collections.Counter()
    for row in rows:
        bid=by_question.get(row['question_id'])
        matches=[bid] if bid is not None else []
        if matches:
            previous=old[bid]
            assert row['db_id']==previous['meta']['db_id']
            assert row['queries'][0]['sql'].strip().removesuffix(';')==previous['queries'][0]['sql'].strip().removesuffix(';')
            split='dev' if bid in wanted['dev'] else 'test';inherited[split]+=1
        else:
            bid='spotit-bird-new-'+str(row['question_id'])
            split='dev' if int(hashlib.sha1(bid.encode()).hexdigest(),16)%10==0 else 'test';new[split]+=1
        assert bid not in seen;seen.add(bid);split_ids[split].append(bid)
        path=Path(row['declared_schema']['path'])
        assert sha(path)==row['declared_schema']['sha256']
        if str(path) not in schemas:
            normalized=Schema.load(str(path));assert normalized.tables
            schemas[str(path)]=sha(path)
        queries=row['queries']
        assert len(queries)==11 and len({q['qid'] for q in queries})==11
        assert all(isinstance(q['sql'],str) and 'role' not in q for q in queries)
        batches.append({'batch_id':bid,'dataset':'bird_public','dialect':'sqlite','schema_path':os.path.relpath(path,OUT),'queries':queries,'meta':{'question_id':row['question_id'],'db_id':row['db_id'],'split':split,'inherited_batch_id':matches[0] if matches else None,'source_batch_id':row['batch_id'],'source_sha256':EXPECTED_POOL_SHA}})
    assert set(split_ids['dev']).isdisjoint(split_ids['test'])
    assert seen==set(split_ids['dev'])|set(split_ids['test'])
    assert dict(inherited)=={'dev':289,'test':1226} or dict(inherited)=={'test':1226,'dev':289}
    assert sum(new.values())==19 and len(schemas)==11
    OUT.mkdir(exist_ok=True)
    target=OUT/'bird_public.jsonl'
    put(target,''.join(json.dumps(r,sort_keys=True,ensure_ascii=False)+'\n' for r in batches))
    for split,ids in split_ids.items():put(OUT/(split+'.ids'),'\n'.join(ids)+'\n')
    pilot=split_ids['dev'][0];put(OUT/'pilot-dev.ids',pilot+'\n')
    # The actual common reader must preserve the full pair population and all identities.
    loaded=dataset(str(target));assert set(loaded)==seen
    eligible={split:sum(len(scored_pairs(loaded[bid])) for bid in ids) for split,ids in split_ids.items()}
    assert sum(eligible.values())>1534*10,'reference-star scoring accidentally retained'
    summary={'state':'runner_input_prepared_not_formal_protocol','dataset':str(target),'dataset_sha256':sha(target),'source_pool_sha256':EXPECTED_POOL_SHA,'source_frozen_dataset_sha256':sha(frozen),'source_dev_ids_sha256':sha(ROOT/'runs/dev_ids/bird_pairs.txt'),'builder_sha256':sha(__file__),'schemas':schemas,'batches':1534,'queries':16874,'split_batches':{s:len(ids) for s,ids in split_ids.items()},'inherited_split_batches':dict(inherited),'new_split_batches':dict(new),'eligible_distinct_sql_pairs':eligible,'pilot_development_batch':pilot,'id_files':{s:sha(OUT/(s+'.ids')) for s in split_ids},'pair_scope':'all pairs with distinct SQL text; repeated identities retained'}
    put(OUT/'runner-input.json',json.dumps(summary,sort_keys=True,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k!='schemas'},sort_keys=True))

if __name__=='__main__':main()
