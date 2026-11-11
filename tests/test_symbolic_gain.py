import itertools
import random
import sys
from pathlib import Path

import pytest
import z3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seer.symbolic import Level
from seer.core import Seer, replay
from seer2.schema import Schema


class FiniteLevel(Level):
    def __init__(self, outputs, valid=None, first=0, unknown_after=None):
        self.outputs = outputs
        self.live = valid or [[True] * len(outputs[0]) for _ in outputs]
        self.x = z3.Int("finite_database")
        self.solver = z3.Solver()
        self.solver.add(self.x >= 0, self.x < len(outputs))
        self.timeout_ms = 2000
        self.deadline = None
        self.checks = 0
        self.solve_s = 0
        self.first = first
        self.unknown_after = unknown_after

    def valid(self, cls):
        return z3.Or([self.x == i for i in range(len(self.outputs)) if all(self.live[i][q] for q in cls)])

    def sep(self, a, b):
        return z3.Or([self.x == i for i, row in enumerate(self.outputs) if row[a] != row[b]])

    def check(self, extra):
        if self.unknown_after is not None and self.checks >= self.unknown_after:
            self.checks += 1
            return z3.unknown, None
        if self.checks == 0 and self.first is not None:
            res, model = super().check(z3.And(extra, self.x == self.first))
            if res == z3.sat:
                return res, model
        return super().check(extra)


def oracle(outputs, valid, classes):
    return max(sum(all(valid[q] for q in c) and row[a] != row[b]
                   for c in classes for a, b in itertools.combinations(c, 2))
               for row, valid in zip(outputs, valid))


def test_representative_swap_can_reduce_pairs_but_max_pairs_keeps_optimum():
    rows = [[0, 0, 0, 1, 1, 1], [0, 1, 0, 1, 0, 0]]
    classes = [list(range(6))]
    old = FiniteLevel(rows)
    _, model, _ = old.find(classes, augment=1)
    assert model.eval(old.x).as_long() == 1
    lv = FiniteLevel(rows)
    status, model, _ = lv.find(classes, selection="max_pairs", budget_s=5)
    assert status == "sat" and model.eval(lv.x).as_long() == 0
    assert lv.selection_info["lower"] == lv.selection_info["upper"] == 9
    assert lv.selection_info["optimal"]


@pytest.mark.parametrize("seed", range(40))
def test_finite_oracle(seed):
    rng = random.Random(seed)
    n = rng.randint(2, 8)
    rows = [[rng.randrange(4) for _ in range(n)] for _ in range(rng.randint(2, 8))]
    live = [[rng.random() > 0.1 for _ in range(n)] for _ in rows]
    split = rng.randint(1, n - 1)
    classes = [list(range(n))] if seed % 2 else [list(range(split)), list(range(split, n))]
    lv = FiniteLevel(rows, live)
    status, model, _ = lv.find(classes, selection="max_pairs", budget_s=5)
    expected = oracle(rows, live, classes)
    assert lv.selection_info["optimal"]
    assert lv.selection_info["lower"] == lv.selection_info["upper"] == expected
    if expected:
        i = model.eval(lv.x).as_long()
        assert status == "sat" and oracle([rows[i]], [live[i]], classes) == expected
    else:
        assert status == "unsat" and model is None


def test_unknown_preserves_feasible_model_and_does_not_claim_optimal():
    lv = FiniteLevel([[0, 0, 1], [0, 1, 2]], unknown_after=1)
    status, model, _ = lv.find([[0, 1, 2]], selection="max_pairs", budget_s=5)
    assert status == "sat" and model.eval(lv.x).as_long() == 0
    assert lv.selection_info["lower"] == 2
    assert lv.selection_info["upper"] == 3
    assert not lv.selection_info["optimal"] and lv.selection_info["stop"] == "unknown"
    assert lv.deadline is None


def test_zero_budget_returns_unknown_without_a_model():
    lv = FiniteLevel([[0, 1]])
    status, model, _ = lv.find([[0, 1]], selection="max_pairs", budget_s=0)
    assert status == "unknown" and model is None
    assert lv.deadline is None


def test_objective_construction_deadline_keeps_initial_model():
    lv = FiniteLevel([[0, 0, 1], [0, 1, 2]])
    def expired():
        raise TimeoutError("objective budget")
    lv._tick = expired
    status, model, _ = lv.find([[0, 1, 2]], selection="max_pairs", budget_s=5)
    assert status == "sat" and model.eval(lv.x).as_long() == 0
    assert lv.selection_info["lower"] == 1
    assert not lv.selection_info["optimal"] and lv.selection_info["stop"] == "budget"


def test_real_sql_certificates_replay():
    schema = Schema("CREATE TABLE employee(id INTEGER PRIMARY KEY, salary INTEGER);", "sqlite")
    queries = [("all", "SELECT COUNT(*) FROM employee"),
               ("nonnull", "SELECT COUNT(salary) FROM employee"),
               ("distinct", "SELECT COUNT(DISTINCT salary) FROM employee")]
    result = Seer(schema, queries, dialect="sqlite", backend="sqlite", fast=False,
                  budget=20, levels=(1, 2), max_k=2, symbolic_selection="max_pairs").run()
    assert result["stop"] == "all_split", result
    assert result["symbolic_selection"] == "max_pairs"
    assert result["symbolic_selection_trace"]
    assert all(v["verdict"] == "split" for v in result["verdicts"].values())
    assert replay(schema, result, dialect="sqlite", backend="sqlite") == len(result["certificates"]["events"])


def test_nontransitive_comparison_counts_pair_predicates_directly():
    class MixedOrdering(FiniteLevel):
        def sep(self, a, b):
            if {a, b} == {0, 2}:
                return self.x == 0
            return z3.BoolVal(False)
    lv = MixedOrdering([[0, 0, 0]])
    status, model, pred = lv.find([[0, 1, 2]], selection="max_pairs", budget_s=5)
    assert status == "sat"
    assert sum(len(b) * (len(b) - 1) // 2 for b in pred[0]) == 1
    assert lv.selection_info["lower"] == lv.selection_info["upper"] == 1
    assert lv.selection_info["optimal"]


def test_representative_unsat_does_not_hide_nontransitive_pair():
    class MixedOrdering(FiniteLevel):
        def sep(self, a, b):
            return z3.BoolVal({a, b} == {0, 2})
    lv = MixedOrdering([[0, 0, 0]])
    status, model, _ = lv.find([[1, 0, 2]], selection="max_pairs", budget_s=5)
    assert status == "sat" and model is not None
    assert lv.selection_info["lower"] == lv.selection_info["upper"] == 1
    assert lv.selection_info["optimal"]


def test_prediction_deadline_keeps_sat_model_with_conservative_blocks():
    lv = FiniteLevel([[0, 1]])
    def expired(*args):
        raise TimeoutError("prediction budget")
    lv._predicted = expired
    status, model, pred = lv.find([[0, 1]], selection="max_pairs", budget_s=5)
    assert status == "sat" and model is not None
    assert pred == [[[0, 1]]]
    assert not lv.selection_info["optimal"] and lv.selection_info["stop"] == "budget"
