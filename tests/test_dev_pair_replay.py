import importlib.util,json,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"eval")]
spec=importlib.util.spec_from_file_location("replay_dev",ROOT/"scripts/replay_dev_pair_evidence.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from seereval.schema import from_ddl


@pytest.mark.parametrize("case",["mixed_order","instance_error"])
def test_replay_preserves_observed_false_partition_splits(tmp_path,case):
    schema=from_ddl("spec","CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);",dialect="sqlite")
    path=tmp_path/"schema.json";schema.dump(path)
    queries={"bridge":"SELECT x FROM t","ascending":"SELECT x FROM t ORDER BY x ASC","descending":"SELECT x FROM t ORDER BY x DESC"} if case=="mixed_order" else {"all":"SELECT x FROM t","none":"SELECT x FROM t WHERE 0","error":"SELECT CASE WHEN COUNT(*) > 1 THEN ABS(-9223372036854775808) ELSE 0 END FROM t"}
    row={"error_class":[],"certs":[{"idx":7,"db":{"t":[{"x":1},{"x":2}]},"cands":[]}],"vpartition":[[q] for q in queries]}
    batch={"dialect":"sqlite","queries":[{"qid":q,"sql":sql} for q,sql in queries.items()]}
    result=m.replay(row,batch,path)
    assert result["state"]=="replayed"
    assert result["n_genuine_pairs"]==1
    assert result["legacy_separated_pairs"]==3
    assert result["legacy_splits_without_replayed_witness"]==2
    assert not result["empty_screen_changed"]
    assert next(p for p in result["pairs"] if p["verdict"]=="split")["certificate"]==7


def test_source_failure_and_missing_certificates_stay_distinct():
    assert m.replay({"runner_error":"timeout"},{},None)["state"]=="source_failure"
    assert m.replay({"runner_error":None},{},None)["state"]=="missing_retained_certificates"
