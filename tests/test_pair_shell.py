import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"eval"))
from seereval.schema import from_ddl
from seereval import pair_shell as shell
S=from_ddl("spec","CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);",dialect="sqlite")
DB={"t":[{"x":1},{"x":2}]}
QUERIES=[{"qid":"u","sql":"SELECT x FROM t"},{"qid":"a","sql":"SELECT x FROM t ORDER BY x"},{"qid":"d","sql":"SELECT x FROM t ORDER BY x DESC"}]


def test_mixed_order_not_inferred_from_bridge_or_native_eq(monkeypatch):
    calls=[]
    def native(tool,st,qs,timeout,seed,**kwargs):
        calls.append(tuple(qs))
        return {"verdict":"neq","instance":DB} if set(qs)=={"a","d"} else {"verdict":"eq"}
    monkeypatch.setattr(shell,"_tool_call",native)
    r=shell.pairwise_graph_shell("sqlwitness",S,"sqlite",QUERIES,10)
    assert calls==[("u","a"),("u","d"),("a","d")]
    assert r["n_genuine_pairs"]==1
    assert [(p["a"],p["b"]) for p in r["pairs"] if p["verdict"]=="split"]==[("a","d")]
    assert all(p["verdict"]=="undecided" for p in r["pairs"] if "u" in (p["a"],p["b"]))


def test_real_cached_witness_skips_only_supported_pairs(monkeypatch):
    calls=[]
    def native(tool,st,qs,timeout,seed,**kwargs):
        calls.append(qs);return {"verdict":"neq","instance":DB}
    monkeypatch.setattr(shell,"_tool_call",native)
    qs=[{"qid":"a","sql":"SELECT x FROM t"},{"qid":"b","sql":"SELECT x FROM t WHERE 0"},{"qid":"c","sql":"SELECT x FROM t WHERE x=1"}]
    r=shell.pairwise_graph_shell("parseval",S,"sqlite",qs,10)
    assert len(calls)==1 and r["n_genuine_pairs"]==3


def test_error_is_not_cached_as_difference(monkeypatch):
    qs=QUERIES[:1]+[{"qid":"e","sql":"SELECT CASE WHEN COUNT(*) > 1 THEN ABS(-9223372036854775808) ELSE 0 END FROM t"}]
    monkeypatch.setattr(shell,"_tool_call",lambda *a,**kw:{"verdict":"neq","instance":DB})
    r=shell.pairwise_graph_shell("sqlwitness",S,"sqlite",qs,10)
    assert r["n_genuine_pairs"]==0 and r["n_dbs"]==0
    assert r["calls"][0]["class"]=="exec_error"


def test_restart_preserves_native_bound_and_seed_progression(monkeypatch):
    calls=[]
    def native(tool,st,qs,timeout,seed,bound=None,**kwargs):
        calls.append((seed,bound))
        return {"verdict":"eq"} if len(calls)<3 else {"verdict":"unsupported"}
    monkeypatch.setattr(shell,"_tool_call",native)
    r=shell.pairwise_graph_shell("verieql",S,"sqlite",QUERIES[:2],10,seed=7,restart=True)
    assert calls==[(7,2),(1007,3),(2007,4)]
    assert r["pairs"][0]["verdict"]=="undecided"


def test_budget_overrun_retained_but_not_accepted(monkeypatch):
    clock=[0.0]
    monkeypatch.setattr(shell.time,"monotonic",lambda:clock[0])
    def native(*a,**kw):clock[0]=11;return {"verdict":"neq","instance":DB}
    monkeypatch.setattr(shell,"_tool_call",native)
    qs=[{"qid":"a","sql":"SELECT x FROM t"},{"qid":"b","sql":"SELECT x FROM t WHERE 0"}]
    r=shell.pairwise_graph_shell("sqlwitness",S,"sqlite",qs,10)
    assert r["n_genuine_pairs"]==0 and r["calls"][0]["class"]=="over_budget"
    assert r["calls"][0]["instance"]==DB


def test_native_multi_query_mode_cannot_silently_become_pairwise():
    with pytest.raises(ValueError):shell.pairwise_graph_shell("xdatan",S,"sqlite",QUERIES,10)


def test_cli_records_failure_contract_and_rejects_legacy_resume(tmp_path,monkeypatch):
    import importlib.util,json
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location("pair_runner",root/"eval/run_baseline.py")
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    schema=tmp_path/"schema.json";S.dump(schema)
    dataset=tmp_path/"data.jsonl"
    dataset.write_text(json.dumps({"batch_id":"spec","dataset":"spec","schema_path":"schema.json","dialect":"sqlite","queries":QUERIES})+"\n")
    output=tmp_path/"result.jsonl"
    args=["run_baseline.py","--tool","sqlwitness","--dataset",str(dataset),"--out",str(output),"--comparison-state","pairs"]
    monkeypatch.setattr(sys,"argv",args)
    monkeypatch.setattr(shell,"_tool_call",lambda *a,**kw:(_ for _ in ()).throw(RuntimeError("native diagnostic failure")))
    runner.main()
    row=json.loads(output.read_text())
    assert row["runner_error"] and row["state_model"]=="pair_evidence"
    assert str(schema) in row["comparison_contract"]["files"]
    runner.main()  # same failed record is retained, not silently replaced with a better trial
    assert len(output.read_text().splitlines())==1
    output.write_text(json.dumps({"batch_id":"spec","partition":[["a"]]})+"\n")
    with pytest.raises(AssertionError,match="refuse mixed legacy"):
        runner.main()


def test_legacy_scorer_cannot_silently_score_graph_output_as_zero(tmp_path):
    import importlib.util,json
    spec=importlib.util.spec_from_file_location("legacy_matrix_score",Path(__file__).resolve().parents[1]/"scripts/matrix_score.py")
    scorer=importlib.util.module_from_spec(spec);spec.loader.exec_module(scorer)
    path=tmp_path/"results.jsonl"
    path.write_text(json.dumps({"batch_id":"spec","partition":[["a"],["b"]]})+"\n")
    assert scorer.records(str(path))["spec"]["partition"]==[["a"],["b"]]
    path.write_text(json.dumps({"batch_id":"spec","state_model":"pair_evidence","pairs":[{"a":"a","b":"b","verdict":"split"}]})+"\n")
    with pytest.raises(ValueError,match="refuse legacy partition inference"):
        scorer.records(str(path))
