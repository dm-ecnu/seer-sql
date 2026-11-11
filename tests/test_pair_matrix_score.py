import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from pair_matrix_score import score_record, summarize
from seereval.schema import from_ddl


SCHEMA = from_ddl("pair_score", "CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);", dialect="sqlite")
QUERIES = [
    {"qid": "u", "sql": "SELECT x FROM t", "role": "reference"},
    {"qid": "a", "sql": "SELECT x FROM t ORDER BY x ASC"},
    {"qid": "d", "sql": "SELECT x FROM t ORDER BY x DESC"},
]


def batch():
    return {"batch_id": "b", "dataset": "fixture", "dialect": "sqlite", "queries": QUERIES,
            "_schema": SCHEMA}


def record():
    return {"batch_id": "b", "dataset": "fixture", "state_model": "pair_evidence",
            "comparison_contract": {"mode": "pair_evidence", "tool": "polygon", "seed": 0, "budget_s": 30},
            "pairs": [{"a": "u", "b": "a", "certificate": None, "verdict": "undecided"},
                      {"a": "u", "b": "d", "certificate": None, "verdict": "undecided"},
                      {"a": "a", "b": "d", "certificate": 0, "verdict": "split"}],
            "error_class": [], "dbs": [{"t": [{"x": 1}, {"x": 2}]}],
            "wall_secs": 2.0, "cpu_secs": 0.4, "cpu_complete": True, "n_calls": 1}


def test_real_sql_witness_does_not_imply_reference_split():
    observed, cost = score_record(batch(), record())
    assert observed == {("u", "a"): "undecided", ("u", "d"): "undecided"}
    assert cost["cpu"] == 0.4


def test_invalid_or_missing_certificate_rejected():
    row = record()
    row["pairs"][0].update(certificate=0, verdict="split")
    with pytest.raises(ValueError, match="unverified"):
        score_record(batch(), row)
    row = record()
    row["pairs"][2]["certificate"] = 2
    with pytest.raises(ValueError, match="index out of range"):
        score_record(batch(), row)


def test_missing_and_crash_are_not_zero_filled():
    assert set(score_record(batch(), None)[0].values()) == {"missing"}
    row = record()
    row["runner_error"] = "timeout"
    assert set(score_record(batch(), row)[0].values()) == {"crash"}


def test_complete_graph_required():
    row = record()
    row["pairs"].pop()
    with pytest.raises(ValueError, match="incomplete pair graph"):
        score_record(batch(), row)


def test_union_known_and_missing_repeat_stays_unreported(tmp_path):
    s = {"b": batch()}
    one = record()
    one["pairs"][0].update(certificate=0, verdict="split")
    one["pairs"][2].update(certificate=None, verdict="undecided")
    # A real distinct bag result for reference against an empty query.
    s["b"]["queries"] = [QUERIES[0], {"qid": "none", "sql": "SELECT x FROM t WHERE 0"}]
    one["pairs"] = [{"a": "u", "b": "none", "certificate": 0, "verdict": "split"}]
    full = tmp_path / "full.jsonl"
    full.write_text(json.dumps(one) + "\n")
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    # Empty file has no source contract and must fail, rather than score a zero.
    with pytest.raises(ValueError, match="invalid pair contract"):
        summarize(s, [("polygon", 0, str(full)), ("parseval", 0, str(empty))])
    score = summarize(s, [("polygon", 0, str(full))])
    assert score["known_neq"] == 1 and score["runs"]["polygon:0"]["recall"] == 1
    assert score["mean_std"]["polygon"] is None
    s["b2"] = dict(s["b"], batch_id="b2")
    incomplete = summarize(s, [("polygon", 0, str(full))])["runs"]["polygon:0"]
    assert incomplete["outcomes"]["missing"] == 1
    assert incomplete["recall"] is None and incomplete["wall_sum_s"] is None
    assert incomplete["cpu_sum_s"] is None and incomplete["calls"] is None
