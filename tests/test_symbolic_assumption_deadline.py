import time
import z3

from seer.symbolic import Level


def test_temporary_assumption_matches_scoped_check_without_mutating_base(monkeypatch):
    x = z3.Int('x')
    level = object.__new__(Level)
    level.solver = z3.Solver()
    level.solver.add(x >= 0, x <= 2)
    level.deadline = time.perf_counter() + 5
    level.timeout_ms = 2000
    level.solve_s = 0.0
    level.checks = 0
    base = tuple(level.solver.assertions())

    def forbidden_push(self):
        raise AssertionError('unbounded solver push must not run')

    monkeypatch.setattr(z3.Solver, 'push', forbidden_push)
    assert level.check(x == 1)[0] == z3.sat
    assert level.check(x == 3)[0] == z3.unsat
    assert level.check(x == 2)[0] == z3.sat
    assert tuple(level.solver.assertions()) == base
    assert level.checks == 3


def test_expired_deadline_skips_solver_assumption_check(monkeypatch):
    level = object.__new__(Level)
    level.solver = z3.Solver()
    level.deadline = time.perf_counter() - 1
    level.timeout_ms = 2000
    level.solve_s = 0.0
    level.checks = 0
    assert level.check(z3.BoolVal(True)) == (z3.unknown, None)
    assert level.checks == 0
