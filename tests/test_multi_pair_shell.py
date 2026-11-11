import sys,itertools
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"eval"))
from seereval.schema import from_ddl
from seereval import multi_pair_shell as shell
S=from_ddl("spec","CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);",dialect="sqlite")
DB={"t":[{"x":1},{"x":2}]}
Q=[{"qid":"u","sql":"SELECT x FROM t"},{"qid":"a","sql":"SELECT x FROM t ORDER BY x"},{"qid":"d","sql":"SELECT x FROM t ORDER BY x DESC"}]


def test_polygon_keeps_multi_api_and_does_not_repeat_resolved_pair(monkeypatch):
    calls=[]
    def native(tool,state,members,timeout,seed,op=None):
        assert tool=="polygon" and op=="multi"
        calls.append(tuple(members))
        return {"verdict":"neq","instance":DB} if len(calls)==1 else {"verdict":"eq"}
    monkeypatch.setattr(shell,"_tool_call",native)
    result=shell.polygon_graph_multi(S,"sqlite",Q,10)
    assert calls==[("u","a","d"),("u","a"),("u","d")]
    assert result["n_genuine_pairs"]==1 and result["native_mode"]=="polygon_disambiguate_multi"
    assert [p for p in result["pairs"] if p["verdict"]=="split"][0]["certificate"]==0


def test_xdatan_keeps_one_to_many_and_verifies_killed_claims(monkeypatch):
    calls=[]
    def native(tool,state,members,timeout,seed,**kw):
        assert tool=="xdatan"
        calls.append(tuple(members))
        return {"verdict":"neq","states":["Killed"]*(len(members)-1),"instances":[DB]}
    monkeypatch.setattr(shell,"_tool_call",native)
    result=shell.xdatan_graph_multi(S,"sqlite",Q,10)
    assert calls[0]==("u","a","d")
    assert len(calls)==3 and len({members[0] for members in calls})==3
    assert result["calls"][0]["claims"]==["a","d"] and result["calls"][0]["backed"]==[]
    assert result["n_genuine_pairs"]==1


def test_invalid_and_error_instances_cannot_back_killed_claim(monkeypatch):
    invalid={"t":[{"x":1},{"x":1}]}
    monkeypatch.setattr(shell,"_tool_call",lambda *a,**kw:{"verdict":"neq","states":["Killed"],"instances":[invalid]})
    result=shell.xdatan_graph_multi(S,"sqlite",Q[:2],10)
    assert result["n_genuine_pairs"]==0 and result["n_dbs"]==0
    assert all(not r["backed"] for r in result["calls"])
    assert result["calls"][0]["datasets"][0]["class"]=="violating"


def test_all_native_datasets_are_processed_not_just_first(monkeypatch):
    qs=[{"qid":"a","sql":"SELECT x FROM t"},{"qid":"b","sql":"SELECT x FROM t WHERE 0"}]
    monkeypatch.setattr(shell,"_tool_call",lambda *a,**kw:{"verdict":"neq","states":["Killed"],"instances":[{"t":[]},DB]})
    result=shell.xdatan_graph_multi(S,"sqlite",qs,10)
    assert len(result["calls"])==1
    assert len(result["calls"][0]["datasets"])==2
    assert result["n_genuine_pairs"]==1 and result["calls"][0]["backed"]==["b"]


def test_multi_budget_overrun_is_preserved_not_counted(monkeypatch):
    clock=[0.0];monkeypatch.setattr(shell.time,"monotonic",lambda:clock[0])
    def native(*a,**kw):clock[0]=11;return {"verdict":"neq","instance":DB}
    monkeypatch.setattr(shell,"_tool_call",native)
    result=shell.polygon_graph_multi(S,"sqlite",Q,10)
    assert result["n_genuine_pairs"]==0
    assert result["calls"][0]["class"]=="over_budget" and result["calls"][0]["instance"]==DB


def test_polygon_original_request_builder_receives_multi_flag(monkeypatch):
    from seereval import tools
    calls=[]
    def native(tool,request,hard_s):
        calls.append(request)
        assert request["op"]=="multi" and len(request["queries"])==3
        assert "tables" in request and "constraints" in request
        return {"verdict":"eq"}
    monkeypatch.setattr(tools,"call",native)
    result=shell.polygon_graph_multi(S,"sqlite",Q,10)
    assert len(calls)==1 and result["n_genuine_pairs"]==0


def test_xdatan_original_request_builder_preserves_query_and_mutant_list(monkeypatch):
    from seereval import tools
    calls=[]
    def native(tool,request,hard_s):
        calls.append(request)
        assert tool=="xdatan" and "q1" in request and "mutants" in request and "ddl" in request
        assert "queries" not in request
        return {"verdict":"unknown","states":["Unknown"]*len(request["mutants"]),"instances":[]}
    monkeypatch.setattr(tools,"call",native)
    result=shell.xdatan_graph_multi(S,"sqlite",Q,10)
    assert len(calls)==3 and len(calls[0]["mutants"])==2
    assert result["n_genuine_pairs"]==0
