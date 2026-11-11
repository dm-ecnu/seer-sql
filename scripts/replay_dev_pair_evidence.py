"""Recheck all retained certificates from the fixed completed development matrix.

No new search, fitting, tool call or change to original records/metrics.
"""
import hashlib,itertools,json,os,resource,subprocess,sys,time
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];BASE=ROOT.parent
sys.path.insert(0,str(ROOT/"eval"));sys.path.insert(0,str(ROOT/"src"))
from seereval.schema import Schema
from seereval.engine import normalize_sql
from seereval.validator import check_db,empty_db_errors
from seereval.pair_evidence import pair_evidence
from seer.core import from_jsonable
SOURCE=BASE/"gain-policy-wt/runs/gain-dev-v1"
OUT=ROOT/"runs/dev-pair-evidence-v1"
DATASETS=("tpcds","leetcode_batches","cubes_d50","cubes_d100")


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def inputs():
    jobs=(SOURCE/"jobs.txt").read_text().splitlines()
    expected={f"{d}:{s}:{p}" for d in DATASETS for s in range(5) for p in ("representatives","max_pairs")}
    assert len(jobs)==40 and set(jobs)==expected
    selected={d:set((SOURCE/"dev_ids"/(d+".txt")).read_text().splitlines()) for d in DATASETS}
    assert {d:len(v) for d,v in selected.items()}==dict(zip(DATASETS,(8,7,3,3)))
    files={"jobs":SOURCE/"jobs.txt"};batches={};records=[]
    for d in DATASETS:
        path=BASE/"datasets/batches"/(d+".jsonl");files["data:"+d]=path
        files["ids:"+d]=SOURCE/"dev_ids"/(d+".txt")
        candidates=[json.loads(line) for line in path.open() if line.strip()]
        batches[d]={b["batch_id"]:b for b in candidates if b["batch_id"] in selected[d]}
        assert set(batches[d])==selected[d]
    for job in jobs:
        d,seed,policy=job.split(":");path=SOURCE/(f"{d}.s{seed}.{policy}.jsonl")
        assert path.with_suffix(".done").exists(),f"source incomplete: {job}"
        files["run:"+job]=path
        rows=[json.loads(line) for line in path.open() if line.strip()]
        assert len(rows)==len(selected[d]) and {r["batch_id"] for r in rows}==selected[d]
        for row in rows:
            b=batches[d][row["batch_id"]]
            schema=(BASE/"datasets/batches"/b["schema_path"]).resolve()
            files["schema:"+d+":"+row["batch_id"]]=schema
            assert row["seed"]==int(seed) and row["budget"]==600
            prov=row["provenance"]
            assert prov["symbolic_selection"]==policy
            assert prov["dataset_sha256"]==sha(files["data:"+d])
            assert prov["selection_ids_sha256"]==sha(files["ids:"+d])
            assert prov["schema_sha256"]==sha(schema)
            records.append((job,path,row,b,schema))
    assert len(records)==210
    files.update({"code:"+str(p.relative_to(ROOT)):p for p in [Path(__file__),ROOT/"eval/seereval/pair_evidence.py",ROOT/"eval/seereval/validator.py",ROOT/"eval/seereval/engine.py",ROOT/"eval/seereval/schema.py"]})
    config={"scope":"fixed development certificates only; original metrics unchanged", "expected_records":210,
            "timeout_per_query_s":30,"source":str(SOURCE),
            "code_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
            "files":{key:{"path":str(p),"sha256":sha(p)} for key,p in files.items()}}
    return records,config


def replay(row,b,schema_path):
    result={"source_runner_error":row.get("runner_error"),"retained_certificates":len(row.get("certs") or []),
            "original_error_class":row.get("error_class",[])}
    if row.get("runner_error") or row.get("certs") is None:
        result["state"]="source_failure" if row.get("runner_error") else "missing_retained_certificates"
        return result
    es=Schema.load(schema_path);dialect=b["dialect"]
    sql={q["qid"]:normalize_sql(q["sql"],dialect) for q in b["queries"]}
    ref=next((q["qid"] for q in b["queries"] if q.get("role")=="reference"),None)
    pairs=[(ref,q) for q in sql if q!=ref] if ref else [(a,c) for a,c in itertools.combinations(sql,2) if sql[a].strip()!=sql[c].strip()]
    errors=empty_db_errors(es,dialect,sql,timeout_s=30)
    excluded=set(errors)|set(row.get("error_class") or [])
    checked_statuses=Counter();validation_errors=[]
    def checks():
        indices=set()
        for cert in row["certs"]:
            idx=cert["idx"];assert idx not in indices;indices.add(idx)
            try:
                check=check_db(es,dialect,from_jsonable(cert["db"]),sql,timeout_s=30,also_bare=False)
                checked_statuses[check["status"]]+=1
                # Empty-schema errors remain in the denominator but cannot earn a split.
                for q in excluded:check["results"].pop(q,None)
                yield idx,check
            except Exception as exc:
                validation_errors.append({"certificate":idx,"error":type(exc).__name__+": "+str(exc)[:350]})
    evidence=pair_evidence(pairs,checks())
    groups={q:i for i,group in enumerate(row.get("vpartition") or []) for q in group}
    old_errors=set(row.get("error_class") or [])
    for pair in evidence["pairs"]:
        a,c=pair["a"],pair["b"]
        pair["legacy_split"]=a not in old_errors and c not in old_errors and a in groups and c in groups and groups[a]!=groups[c]
    old=sum(p["legacy_split"] for p in evidence["pairs"])
    unsupported=sum(p["legacy_split"] and p["verdict"]!="split" for p in evidence["pairs"])
    recovered=sum(not p["legacy_split"] and p["verdict"]=="split" for p in evidence["pairs"])
    result.update(state="replayed" if not validation_errors else "validation_incomplete",reference=ref,
                  current_error_class=errors,empty_screen_changed=set(errors)!=old_errors,
                  legacy_separated_pairs=old,legacy_splits_without_replayed_witness=unsupported,
                  witnessed_pairs_not_in_legacy_partition=recovered,validation_errors=validation_errors,**evidence)
    return result


if __name__=="__main__":
    resource.setrlimit(resource.RLIMIT_AS,(8*1024**3,8*1024**3))
    records,config=inputs();OUT.mkdir(parents=True,exist_ok=True)
    path=OUT/"config.json"
    if path.exists():assert json.loads(path.read_text())==config,"inputs changed; preserve prior attempt"
    else:path.write_text(json.dumps(config,indent=2)+"\n")
    config_sha=sha(path);out=OUT/"results.jsonl"
    previous=[json.loads(line) for line in out.open()] if out.exists() else []
    assert all(r["config_sha256"]==config_sha for r in previous)
    done={(r["job"],r["batch_id"]) for r in previous};assert len(done)==len(previous)
    with out.open("a") as stream:
        for job,source,row,b,schema in records:
            key=(job,row["batch_id"])
            if key in done:continue
            started=time.time()
            record={"job":job,"batch_id":row["batch_id"],"source":str(source),"config_sha256":config_sha}
            try:record.update(replay(row,b,schema))
            except Exception as exc:record.update(state="replay_failure",error=type(exc).__name__+": "+str(exc)[:500])
            record["replay_wall_s"]=time.time()-started
            stream.write(json.dumps(record,default=str)+"\n");stream.flush()
            print(json.dumps({k:record.get(k) for k in ("job","batch_id","state","retained_certificates","legacy_splits_without_replayed_witness","replay_wall_s")}),flush=True)
    rows=[json.loads(line) for line in out.open()]
    assert len(rows)==210 and {(r["job"],r["batch_id"]) for r in rows}=={(j,r["batch_id"]) for j,_,r,_,_ in records}
    summary={"records":210,"states":dict(Counter(r["state"] for r in rows)),"config_sha256":config_sha,
             "source_metrics_replaced":False,"scope":"development certificate replay; not formal timing or main-table scoring"}
    (OUT/"summary.json").write_text(json.dumps(summary,indent=2)+"\n");print(json.dumps(summary),flush=True)
