"""The shared counterexample validator (eval/seereval), pinned in this repo for the opponent matrix."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
from seereval.engine import pyformat_escape, make_engine  # noqa: E402
from seereval.schema import Schema, Table  # noqa: E402
from seereval.validator import check_db, classify_from_check  # noqa: E402


def _schema():
    return Schema("pct", [Table("t", [{"name": "id", "type": "int", "nullable": False},
                                      {"name": "Pct (%) x", "type": "int", "nullable": True}], ["id"])])


def test_percent_escape():
    assert pyformat_escape('INSERT INTO "t" ("Pct (%) x")') == 'INSERT INTO "t" ("Pct (%%) x")'


@pytest.mark.parametrize("dialect", ["postgresql", "mysql", "sqlite"])
def test_validator_loads_percent_identifier(dialect):
    try:
        make_engine(dialect, _schema()).close()
    except Exception:
        pytest.skip(f"no {dialect} here")
    q = {"a": 'SELECT "Pct (%) x" FROM t' if dialect != "mysql" else "SELECT `Pct (%) x` FROM t",
         "b": "SELECT id FROM t"}
    chk = check_db(_schema(), dialect, {"t": [{"id": 1, "Pct (%) x": 3}]}, q)
    assert classify_from_check(chk, "a", "b")["class"] == "genuine", chk


def test_shell_restarts_until_budget(monkeypatch):
    """With restart=True a call without a genuine counterexample is repeated (fresh seed, next bound) within the
    batch budget; without it the pair keeps the first verdict."""
    from seereval import shell
    calls = []

    def fake(tool, st, qs, timeout, seed, op="pair", bound=None):
        calls.append((seed, bound))
        if len(calls) < 3:
            return {"verdict": "eq", "wall": 0.0}
        return {"verdict": "neq", "wall": 0.0, "instance": {"t": [{"id": 1, "Pct (%) x": 5}]}}

    monkeypatch.setattr(shell, "_tool_call", fake)
    qs = [{"qid": "a", "sql": 'SELECT "Pct (%) x" FROM t'}, {"qid": "b", "sql": "SELECT id FROM t"}]
    r = shell.pairwise_shell("verieql", _schema(), "sqlite", qs, budget_s=30, restart=True)
    assert sorted(map(sorted, r["partition"])) == [["a"], ["b"]]
    assert [b for _, b in calls] == [2, 3, 4] and len({s for s, _ in calls}) == 3
    calls.clear()
    r = shell.pairwise_shell("verieql", _schema(), "sqlite", qs, budget_s=30, restart=False)
    assert r["partition"] == [["a", "b"]] and len(calls) == 1


def test_shell_restarts_round_robin(monkeypatch):
    """Restarts begin only after one pass over the batch and then cycle over every unsplit pair, so a pair that
    never splits cannot starve the others of the batch budget."""
    from seereval import shell
    calls = []

    def fake(tool, st, qs, timeout, seed, op="pair", bound=None):
        calls.append((tuple(qs), seed))
        if seed >= 1000 and "c" in qs:
            return {"verdict": "neq", "wall": 0.0, "instance": {"t": [{"id": 1, "Pct (%) x": 5}]}}
        return {"verdict": "eq", "wall": 0.0}

    monkeypatch.setattr(shell, "_tool_call", fake)
    qs = [{"qid": "a", "sql": 'SELECT "Pct (%) x" FROM t'}, {"qid": "b", "sql": 'SELECT "Pct (%) x" FROM t'},
          {"qid": "c", "sql": "SELECT id FROM t"}]
    r = shell.pairwise_shell("sqlwitness", _schema(), "sqlite", qs, budget_s=2, restart=True)
    assert sorted(map(sorted, r["partition"])) == [["a", "b"], ["c"]]
    assert calls[:4] == [(("a", "b"), 0), (("a", "c"), 0), (("a", "b"), 1000), (("a", "c"), 1000)]


def test_cpu_wrap_records_child_cpu(tmp_path):
    """The tool-call wrapper reports the CPU seconds of the worker it ran (efficiency is reported in CPU time)."""
    import subprocess
    import sys
    out = tmp_path / "cpu.txt"
    wrap = os.path.join(os.path.dirname(__file__), "..", "eval", "tools", "cpu_wrap.py")
    burn = "import time\nt=time.process_time()\nwhile time.process_time()-t<0.3: pass\nprint('ok')"
    r = subprocess.run([sys.executable, wrap, str(out), "--", sys.executable, "-c", burn], capture_output=True,
                       text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok"
    cpu, rq = map(float, out.read_text().split())
    assert 0.25 <= cpu < 5 and rq >= 0
