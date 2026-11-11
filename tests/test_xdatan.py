"""XDataN adapter: dataset parsing, the batch driver around XDataN (shell.xdatan_multi), CHECK parsing of the
benchmark DDL, and the scorer's treatment of XDataN's Killed claims and the benchmark authors' labels."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from seereval.schema import Schema, Table, from_ddl  # noqa: E402


def _schema():
    return Schema("s", [Table("t", [{"name": "id", "type": "int", "not_null": True},
                                    {"name": "v", "type": "int", "not_null": False},
                                    {"name": "s", "type": "varchar", "not_null": False}], ["id"])])


def test_parse_dataset(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setitem(sys.modules, "wproto", type(sys)("wproto"))
    sys.modules["wproto"].serve = lambda h: None
    xw = importlib.import_module("xdatan_worker")
    f = tmp_path / "DS0.sql"
    f.write_text("delete from t\ninsert into t values ('1','-2.0','it''s')\n"
                 "-- JSQ0 values ('9')\ninsert into T values ('2',null,'x,y')\n")
    inst = xw.parse_dataset(str(f), {"t": ["id", "v", "s"]}, {"t": {"id": "int", "v": "int", "s": "varchar"}})
    assert inst == {"t": [{"id": 1, "v": -2, "s": "it's"}, {"id": 2, "v": None, "s": "x,y"}]}
    g = tmp_path / "DS1.sql"
    g.write_text("insert into t values ('3','4')\n")        # XDataN dropped an empty-string value
    assert xw.parse_dataset(str(g), {"t": ["id", "v", "s"]},
                            {"t": {"id": "int", "v": "int", "s": "varchar"}}) == {"t": [{"id": 3, "v": 4, "s": None}]}
    assert xw.one_line("SELECT a -- c\n FROM t\n WHERE s = 'x  -- y';") == "SELECT a FROM t WHERE s = 'x  -- y'"


def test_xdatan_multi_uses_each_query_once(monkeypatch):
    """Datasets are validated and cached; a class that is not split is retried with its next member as XDataN's
    query; Killed claims without a separating dataset are kept apart from backed ones."""
    from seereval import shell, tools
    seen = []

    def fake_call(tool, req, hard_s, mem_gb=None):
        seen.append(req["q1"])
        if req["q1"].endswith("v > 0"):          # q1's datasets: one separates q2 from q1, one separates nothing
            return {"verdict": "neq", "states": ["Killed", "Killed"],
                    "instances": [{"t": [{"id": 1, "v": 0, "s": None}]}, {"t": [{"id": 1, "v": 5, "s": None}]}]}
        return {"verdict": "neq", "states": ["NotKilled"], "instances": [{"t": [{"id": 1, "v": 5, "s": None}]}]}

    monkeypatch.setattr(tools, "call", fake_call)
    qs = [{"qid": "q1", "sql": "SELECT id FROM t WHERE v > 0"}, {"qid": "q2", "sql": "SELECT id FROM t"},
          {"qid": "q3", "sql": "SELECT id FROM t WHERE v >= 1"}]
    r = shell.xdatan_multi(_schema(), "sqlite", qs, budget_s=30)
    assert sorted(map(sorted, r["partition"])) == [["q1", "q3"], ["q2"]]
    assert r["n_dbs"] == 1
    first = r["calls"][0]
    assert first["rep"] == "q1" and first["claims"] == ["q2", "q3"] and first["backed"] == ["q2"]
    assert first["dataset_outcomes"] == {"genuine": 1, "spurious": 1}
    assert [c["rep"] for c in r["calls"]] == ["q1", "q3"] and len(seen) == 2


def test_from_ddl_checks():
    s = from_ddl("u", "create table section (sec_id varchar(8), semester varchar(6) check (semester in "
                      "('Fall', 'Spring')), year numeric(4,0) check (year > 1701 and year < 2100), "
                      "primary key (sec_id));")
    assert s.enums == [{"table": "section", "column": "semester", "values": ["Fall", "Spring"]}]
    assert [c["expr"] for c in s.checks] == ["year > 1701", "year < 2100"]


def test_score_xdatan_claims_and_author_labels(tmp_path):
    import matrix_score as ms
    ds = tmp_path / "xdatabm.jsonl"
    ds.write_text(json.dumps({"batch_id": "xdatabm-q001", "dataset": "xdatabm", "queries": [
        {"qid": "orig", "sql": "a", "role": "reference"},
        {"qid": "m0", "sql": "b", "role": "candidate", "author_label": "neq"},
        {"qid": "m1", "sql": "c", "role": "candidate", "author_label": "neq"}]}) + "\n")
    B = ms.load_dataset(str(ds))
    assert B["xdatabm-q001"]["author_neq"] == {("orig", "m0"), ("orig", "m1")}
    rec = {"batch_id": "xdatabm-q001", "tool": "xdatan", "partition": [["orig", "m1"], ["m0"]], "error_class": [],
           "calls": [{"rep": "orig", "members": ["orig", "m0", "m1"], "verdict": "neq", "claims": ["m0", "m1"],
                      "backed": ["m0"], "outcome": "genuine", "t": 1.0}], "wall_secs": 2.0, "budget": 600}
    out, pb = ms.score_run(B, {"xdatabm-q001": rec}, "xdatan")
    assert out[("xdatabm-q001", "orig", "m0")] == "sep"
    assert out[("xdatabm-q001", "orig", "m1")] == "invalid_cert"
    assert pb[0]["certs"] == (2, 1)


def test_score_infra_failure_is_not_a_miss(tmp_path):
    import matrix_score as ms
    ds = tmp_path / "xdatabm.jsonl"
    ds.write_text(json.dumps({"batch_id": "xdatabm-q002", "dataset": "xdatabm", "queries": [
        {"qid": "gold", "sql": "a", "role": "reference"}, {"qid": "cand", "sql": "b", "role": "candidate"}]}) + "\n")
    B = ms.load_dataset(str(ds))
    rec = {"batch_id": "xdatabm-q002", "runner_error": "QueryCanceled: canceling statement due to statement timeout"}
    out, _ = ms.score_run(B, {"xdatabm-q002": rec}, "parseval")
    assert set(out.values()) == {"infra"}
    rec2 = {"batch_id": "xdatabm-q002", "runner_error": "ValueError: tool bug"}
    out2, _ = ms.score_run(B, {"xdatabm-q002": rec2}, "parseval")
    assert set(out2.values()) == {"crash"}


def test_infra_rerun_scan(tmp_path):
    import infra_rerun as ir
    rows = [{"batch_id": "a", "runner_error": "psycopg2.errors.QueryCanceled: statement timeout"},
            {"batch_id": "b", "runner_error": "QueryCanceled"}, {"batch_id": "b", "runner_error": None},
            {"batch_id": "c", "runner_error": "ValueError: x"}, {"batch_id": "d", "runner_error": None}]
    (tmp_path / "spider_pairs.parseval.s3.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "spider_pairs.seer.s3.1.jsonl").write_text(json.dumps(rows[0]) + "\n")
    lost, n = ir.scan(str(tmp_path))
    assert n == 6
    assert set(lost[("spider_pairs", "parseval", 3)]) == {"a"}
    assert set(lost[("spider_pairs", "seer", 3)]) == {"a"}


def test_score_drops_superseded_runs(tmp_path, monkeypatch, capsys):
    import matrix_score as ms
    ds = tmp_path / "xdatabm.jsonl"
    ds.write_text(json.dumps({"batch_id": "xdatabm-q003", "dataset": "xdatabm", "queries": [
        {"qid": "gold", "sql": "a", "role": "reference"}, {"qid": "cand", "sql": "b", "role": "candidate"}]}) + "\n")
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(), new.mkdir()
    rec = {"batch_id": "xdatabm-q003", "partition": [["gold"], ["cand"]], "error_class": [], "calls": [],
           "wall_secs": 1.0, "budget": 600}
    (old / "xdatabm.seer.s0.0.jsonl").write_text(json.dumps(rec) + "\n")
    (old / "xdatabm.parseval.s0.jsonl").write_text(json.dumps(rec) + "\n")
    (new / "xdatabm.seer.s0.0.jsonl").write_text(json.dumps(dict(rec, partition=[["gold", "cand"]])) + "\n")
    out = tmp_path / "o.json"
    monkeypatch.setattr(sys, "argv", ["x", "--dataset", str(ds), "--runs", f"{old},{new}", "--split", "all",
                                      "--drop", f"{old}:seer,v2", "--json", str(out)])
    ms.main()
    r = json.load(open(out))
    assert set(r["methods"]) == {"seer", "parseval"}
    assert r["known_neq"] == 1                      # only parseval's split; the superseded SEER record is ignored
