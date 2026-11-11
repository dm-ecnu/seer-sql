"""Source-preserving nested prefixes; no solver-dependent workload selection."""
import hashlib,json,os,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'eval'),str(ROOT/'src')]
from matrix_score import is_dev
from pair_matrix_score import dataset,scored_pairs
BASE=ROOT.parent
OUT=BASE/'datasets/batches/leetcode-scale-v1'
SIZES=(10,25,50,100,200,500)
SOURCE_SHA='346a6b0805d3a0e3ede66ef9956b83101402242db38cb5dda21243b4f187a47a'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def put(p,data):
    data=data.encode()
    if p.exists():assert p.read_bytes()==data,('immutable input changed',str(p))
    else:
        with p.open('xb') as f:f.write(data)
def main():
    source=BASE/'datasets/batches/leetcode_batches.jsonl'
    assert sha(source)==SOURCE_SHA
    OUT.mkdir(parents=True,exist_ok=True)
    rows=[];groups={'dev':[],'test':[]};schema_files={};parent_info={}
    for original in map(json.loads,source.read_text().splitlines()):
        bid=original['batch_id'];unique={}
        for index,q in enumerate(original['queries']):
            unique.setdefault(q['sql'].strip(),(index,q))
        if len(unique)<500:continue
        split='dev' if is_dev('leetcode_batches',bid) else 'test'
        def key(item):
            index,q=item
            return hashlib.sha256(('seer-lc-scale-v1\0'+bid+'\0'+q['qid']+'\0'+q['sql']).encode()).hexdigest(),index
        chosen=sorted(unique.values(),key=key)
        schema=(source.parent/original['schema_path']).resolve()
        schema_files[str(schema)]=sha(schema)
        parent_info[bid]={'split':split,'source_queries':len(original['queries']),'distinct_sql':len(unique),'source_schema':str(schema),'schema_sha256':sha(schema)}
        for n in SIZES:
            new_id=bid+'-n'+str(n)
            queries=[]
            for index,q in chosen[:n]:
                item={k:v for k,v in q.items() if k!='role'}
                item['source_index']=index
                if 'role' in q:item['source_role']=q['role']
                queries.append(item)
            assert len({q['qid'] for q in queries})==n
            assert len({q['sql'].strip() for q in queries})==n
            assert all('role' not in q for q in queries)
            rows.append({'batch_id':new_id,'dataset':'leetcode_scale','dialect':original['dialect'],
                'schema_path':os.path.relpath(schema,OUT),'queries':queries,
                'meta':{'parent_batch_id':bid,'split':split,'n':n,'source_sha256':SOURCE_SHA,'source_meta':original.get('meta',{}),'candidate_source':'published human LeetCode solutions; not LLM samples'}})
            groups[split].append(new_id)
    assert len(parent_info)==19 and sum(v['split']=='dev' for v in parent_info.values())==1
    assert parent_info['leetcode-1084']['split']=='dev'
    assert len(rows)==114 and len(groups['test'])==108 and len(groups['dev'])==6
    for bid in parent_info:
        prefixes=[r for r in rows if r['meta']['parent_batch_id']==bid]
        assert [len(r['queries']) for r in prefixes]==list(SIZES)
        for a,b in zip(prefixes,prefixes[1:]):assert a['queries']==b['queries'][:len(a['queries'])]
    dest=OUT/'leetcode_scale.jsonl'
    put(dest,''.join(json.dumps(r,sort_keys=True,ensure_ascii=False)+'\n' for r in rows))
    for split,ids in groups.items():put(OUT/(split+'.ids'),'\n'.join(ids)+'\n')
    put(OUT/'pilot-dev.ids','leetcode-1084-n500\n')
    for row in rows:put(OUT/(row['batch_id']+'.ids'),row['batch_id']+'\n')
    loaded=dataset(str(dest));assert set(loaded)=={r['batch_id'] for r in rows}
    pairs={s:sum(len(scored_pairs(loaded[bid])) for bid in ids) for s,ids in groups.items()}
    assert pairs=={'dev':151170,'test':2721060}
    summary={'version':'leetcode-scale-v1','source_file':str(source),'source_sha256':sha(source),
       'builder_sha256':sha(__file__),'dataset':str(dest),'dataset_sha256':sha(dest),
       'sizes':list(SIZES),'parents':parent_info,'schemas':schema_files,'split_batches':{s:len(ids) for s,ids in groups.items()},
       'eligible_pairs_per_repeat':pairs,'query_identities':sum(len(r['queries']) for r in rows),
       'id_files':{s:sha(OUT/(s+'.ids')) for s in ('dev','test','pilot-dev')},
       'candidate_order':'sha256(seer-lc-scale-v1 NUL parent_id NUL original_qid NUL original_sql), original_index tie break; first exact stripped duplicate retained',
       'semantics':'original MySQL/schema/SQL; all distinct SQL pairs; original reference role retained only as provenance',
       'scope':'human solution scaling; not LLM pool; test not inspected for method selection'}
    put(OUT/'input.json',json.dumps(summary,sort_keys=True,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k not in ('parents','schemas')},sort_keys=True))
if __name__=='__main__':main()
