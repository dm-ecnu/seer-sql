import json,sys,time,subprocess
from pathlib import Path
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/"eval"),str(Path(__file__).resolve().parents[1]/"eval/tools")]
from seereval import tools
from execution_deadline import deadline_command


def test_official_outer_timeout_does_not_spend_grace(tmp_path,monkeypatch):
    worker=tmp_path/"slow.py"
    worker.write_text("import sys,json,time\nr=json.load(sys.stdin)\ntime.sleep(2)\nprint(json.dumps({\"verdict\":\"eq\"}))\n")
    (tmp_path/"cpu_wrap.py").symlink_to(Path(tools.WORKERS)/"cpu_wrap.py")
    monkeypatch.setattr(tools,"WORKERS",str(tmp_path))
    monkeypatch.setitem(tools.TOOLS,"sqldriller_official",dict(py=sys.executable,worker=worker.name,pythonpath=None,cwd=None,mem_gb=1,external_cpu=True))
    started=time.monotonic()
    result=tools.call("sqldriller_official",{"timeout":0.3},hard_s=45.3)
    assert result["verdict"]=="timeout",result
    assert not result["cpu_complete"],result
    assert time.monotonic()-started<1.5,result


def test_external_deadline_stops_descendant_before_side_effect(tmp_path):
    marker=tmp_path/"leak"
    code="import pathlib,time;time.sleep(0.8);pathlib.Path("+repr(str(marker))+").touch()"
    started=time.monotonic()
    result=subprocess.run(deadline_command([sys.executable,"-c",code],started+0.4))
    assert result.returncode==124,result
    assert time.monotonic()-started<0.8
    time.sleep(0.85)
    assert not marker.exists()


def test_expired_deadline_never_starts_command():
    with pytest.raises(TimeoutError):deadline_command(["true"],time.monotonic()-1)


def test_external_cap_preserves_completed_output():
    result=subprocess.run(deadline_command([sys.executable,"-c","print(42)"],time.monotonic()+2),capture_output=True,text=True)
    assert result.returncode==0 and result.stdout.strip()=="42"


@pytest.mark.parametrize("expires_before_check",[True,False])
def test_official_never_accepts_certificate_after_batch_budget(monkeypatch,expires_before_check):
    from seereval import shell
    from seereval.schema import from_ddl
    clock=[0.0]
    monkeypatch.setattr(shell.time,"time",lambda:clock[0])
    instance={"t":[{"id":1}]}
    def fake_call(*args,**kwargs):
        clock[0]=4.0 if expires_before_check else 0.1
        return {"verdict":"neq","instance":instance}
    original=shell._State.run_all
    def validate(st,inst):
        assert not expires_before_check,"expired result was validated"
        result=original(st,inst)
        clock[0]=4.0
        return result
    monkeypatch.setattr(shell,"_tool_call",fake_call)
    monkeypatch.setattr(shell._State,"run_all",validate)
    schema=from_ddl("s","CREATE TABLE t(id INTEGER PRIMARY KEY)",dialect="sqlite")
    queries=[{"qid":"a","sql":"SELECT id FROM t"},{"qid":"b","sql":"SELECT id+1 FROM t"}]
    result=shell.pairwise_shell("sqldriller_official",schema,"sqlite",queries,budget_s=3)
    assert result["partition"]==[["a","b"]],result
    assert result["calls"][0]["class"]=="over_budget",result


@pytest.mark.parametrize("raises",[False,True])
def test_call_workspace_is_private_and_host_cleans_it(tmp_path,monkeypatch,raises):
    monkeypatch.setattr(tools,"ROOT",str(tmp_path))
    paths=[]
    def fake_call(tool,req,*args):
        path=Path(req["_call_workspace"])
        assert path.is_dir()
        assert path not in paths
        paths.append(path)
        (path/"runtime-cache").write_text("new values")
        if raises:raise RuntimeError("interrupted")
        return {"verdict":"eq","cpu":0}
    monkeypatch.setattr(tools,"_call",fake_call)
    for _ in range(2):
        if raises:
            with pytest.raises(RuntimeError):tools.call("sqldriller_official",{"timeout":1},1)
        else:tools.call("sqldriller_official",{"timeout":1},1)
    assert len(paths)==2 and all(not path.exists() for path in paths)
