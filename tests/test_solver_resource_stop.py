import pytest
import z3
from seer.core import Seer,replay
from seer2.schema import Schema


def instance(monkeypatch):
 schema=Schema("CREATE TABLE t(a INTEGER);")
 queries=[("gold","SELECT a FROM t"),("different","SELECT a+1 FROM t"),("open","SELECT a FROM t WHERE a>0")]
 seer=Seer(schema,queries,dialect="sqlite",backend="sqlite",budget=5,prover=lambda *a:pytest.fail("prover ran after OOM"))
 monkeypatch.setattr(seer,"fast_tier",lambda:seer.run_db({"t":[{"a":1}]},"test"))
 monkeypatch.setattr(seer,"_close",lambda *a:pytest.fail("search resumed after OOM"))
 return schema,seer


def fail(message):
 def raise_error():raise z3.Z3Exception(message)
 return raise_error


def test_solver_oom_retains_only_replayed_certificates(monkeypatch):
 schema,seer=instance(monkeypatch)
 monkeypatch.setattr(seer,"symbolic_tier",fail("out of memory"))
 result=seer.run()
 assert result["stop"]=="resource_limit"
 assert result["stats"]["solver_out_of_memory"]==1
 assert result["verdicts"]["gold|different"]["verdict"]=="split"
 assert result["verdicts"]["gold|open"]=={"verdict":"undecided","reason":"solver_out_of_memory"}
 assert result["proved"]==[]
 assert result["stats"]["recheck_ok"]==1
 assert replay(schema,result,dialect="sqlite",backend="sqlite")==1


def test_other_solver_errors_still_fail(monkeypatch):
 _,seer=instance(monkeypatch)
 monkeypatch.setattr(seer,"symbolic_tier",fail("sort mismatch"))
 with pytest.raises(z3.Z3Exception,match="sort mismatch"):seer.run()


def test_failed_engine_replay_withdraws_earlier_split(monkeypatch):
 _,seer=instance(monkeypatch)
 original=seer.fast_tier
 def initial_then_alter():
  original()
  # Independent replay now executes equal queries: the earlier event must be withdrawn.
  seer.prep["different"]=seer.prep["gold"]
 monkeypatch.setattr(seer,"fast_tier",initial_then_alter)
 monkeypatch.setattr(seer,"symbolic_tier",fail("out of memory"))
 result=seer.run()
 assert result["stop"]=="resource_limit"
 assert result["verdicts"]["gold|different"]["verdict"]=="undecided"
 assert result["certificates"]["events"]==[]
 assert result["stats"]["recheck_dropped"]==1
