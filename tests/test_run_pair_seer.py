import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_pair_seer import run_batch
from pair_matrix_score import score_record
from seereval.schema import from_ddl


def test_pair_seer_runner_and_independent_scorer_on_actual_sqlite(tmp_path):
    schema = from_ddl("pair_runner", "CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);", dialect="sqlite")
    (tmp_path / "schema.json").write_text(json.dumps(schema.to_json()))
    batch = {"batch_id": "pair-spec", "dataset": "fixture", "dialect": "sqlite", "schema_path": "schema.json",
             "queries": [{"qid": "all", "sql": "SELECT x FROM t", "role": "reference"},
                         {"qid": "none", "sql": "SELECT x FROM t WHERE 0"}]}
    args = SimpleNamespace(budget=2.0, seed=0, symbolic_selection="representatives")
    result = run_batch(batch, args, str(tmp_path))
    assert result["state_model"] == "pair_evidence"
    assert result["pairs"][0]["verdict"] == "split"
    assert result["n_genuine_pairs"] == 1
    batch["_schema"] = schema
    outcomes, _ = score_record(batch, result)
    assert outcomes == {("all", "none"): "split"}
