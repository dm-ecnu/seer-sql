import itertools,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"eval"))
from seereval.schema import from_ddl
from seereval.validator import check_db
from seereval.pair_evidence import pair_evidence


def checked(queries, rows):
    schema=from_ddl("pair_contract","CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);",dialect="sqlite")
    return check_db(schema,"sqlite",{"t":rows},queries)


def test_mixed_order_counts_only_real_separation_and_keeps_same_unknown():
    queries={"u":"SELECT x FROM t","a":"SELECT x FROM t ORDER BY x ASC","d":"SELECT x FROM t ORDER BY x DESC"}
    result=pair_evidence(list(itertools.combinations(queries,2)),[(0,checked(queries,[{"x":1},{"x":2}]))])
    assert result["n_genuine_pairs"]==1
    assert [r for r in result["pairs"] if r["verdict"]=="split"]==[{"a":"a","b":"d","certificate":0,"same_observations":0,"execution_failures":0,"verdict":"split"}]
    assert all(r["verdict"]=="undecided" for r in result["pairs"] if "u" in (r["a"],r["b"]))


def test_instance_error_does_not_split_or_block_unrelated_pair():
    queries={"all":"SELECT x FROM t","none":"SELECT x FROM t WHERE 0","error":"SELECT CASE WHEN COUNT(*) > 1 THEN ABS(-9223372036854775808) ELSE 0 END FROM t"}
    result=pair_evidence(itertools.combinations(queries,2),[(4,checked(queries,[{"x":1},{"x":2}]))])
    assert result["n_genuine_pairs"]==1
    assert sum(r["execution_failures"] for r in result["pairs"])==2


def test_invalid_database_never_counts_and_later_valid_witness_can_count():
    queries={"a":"SELECT x FROM t","b":"SELECT x FROM t WHERE 0"}
    invalid=checked(queries,[{"x":1},{"x":1}]);assert invalid["status"]=="violating"
    good=checked(queries,[{"x":1}])
    result=pair_evidence([("a","b")],[(0,invalid),(1,good),(2,good)])
    assert result["n_genuine_pairs"]==1 and result["pairs"][0]["certificate"]==1
    assert result["checked_database_statuses"]=={"violating":1,"valid":2}


def test_missing_result_is_unknown_and_empty_checks_do_not_prove_equivalence():
    result=pair_evidence([("a","b")],[(0,{"status":"valid","violations":[],"results":{"a":("B",())}})])
    assert result["pairs"][0]["execution_failures"]==1
    assert result["n_genuine_pairs"]==0
    assert pair_evidence([("a","b")],[])["pairs"][0]["verdict"]=="undecided"


def test_duplicate_pair_or_database_is_rejected():
    with pytest.raises(AssertionError):pair_evidence([("a","b"),("b","a")],[])
    item={"status":"valid","violations":[],"results":{}}
    with pytest.raises(AssertionError):pair_evidence([("a","b")],[(0,item),(0,item)])
