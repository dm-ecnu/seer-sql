"""At most three partition-wide extensions on the existing class context."""
import time
import z3
from z3.z3util import get_vars


def complete_order_choices(level, model, clauses):
    """Extend the SAT model only with newly allocated ORDER/LIMIT choices.

    Query outputs are expressions over existing row variables. The encoder's
    only query-local auxiliary constraints are bounded, distinct order ranks.
    Give new ranks a legal permutation without changing any existing value;
    validate every added clause before evaluating representative separations.
    This avoids encoding unrelated representatives on an UNSAT block and does
    not consume an extra solver call merely to complete auxiliary values.
    """
    def check_time():
        if time.perf_counter()>=level.deadline:raise TimeoutError('objective encoding deadline')
    for clause in clauses:
        check_time()
        if clause.decl().kind()!=z3.Z3_OP_DISTINCT:continue
        variables=clause.children();used=set()
        for v in variables:
            value=model.get_interp(v.decl())
            if value is not None:used.add(value.as_long())
        free=iter(i for i in range(len(variables)) if i not in used)
        for v in variables:
            if model.get_interp(v.decl()) is None:
                assert str(v.decl().name()).startswith(level.enc.pfx+'_tie'), 'unexpected objective auxiliary'
                model.update_value(v,z3.IntVal(next(free)))
    for clause in clauses:
        check_time()
        for v in get_vars(clause):
            if model.get_interp(v.decl()) is None:
                assert z3.is_int(v) and str(v.decl().name()).startswith(level.enc.pfx+'_tie'), 'unexpected objective auxiliary'
                model.update_value(v,z3.IntVal(0))
        assert z3.is_true(model.eval(clause,model_completion=True)), 'objective auxiliary completion violates encoding'


def extend_model(level, model, block_targets, representative_targets):
    """Keep every established separation; failed checks retain the last model.

    Representative expressions use the original row assignment and completed
    legal ORDER/LIMIT choices; model evaluation cannot invent auxiliary values.
    """
    terms = {('block', q): t for q, t in block_targets.items()}
    terms.update({('representative', a, b): t
                  for (a, b), t in representative_targets.items()})
    satisfied = lambda m: {key for key, term in terms.items()
                          if z3.is_true(m.eval(term, model_completion=True))}
    keep = satisfied(model)
    assert any(key[0] == 'block' for key in keep), 'objective lost initial block'
    initial = set(keep)
    rounds = []
    history = [sorted(keep)]
    for _ in range(3):
        remaining = [key for key in terms
                     if key[0] == 'representative' and key not in keep]
        if not remaining or level.deadline <= time.perf_counter():
            break
        goal = z3.And(*[terms[key] for key in keep],
                      z3.Or(*[terms[key] for key in remaining]))
        before = level.checks
        answer, candidate = level.check(goal)
        if level.checks > before:
            rounds.append(dict(result=str(answer),
                               unknown_reason=level.last_unknown_reason))
        if answer != z3.sat:
            break
        gained = satisfied(candidate)
        assert keep <= gained, 'objective K is not monotone'
        assert any(key in gained for key in remaining), 'objective SAT added no separation'
        assert any(key[0] == 'block' for key in gained), 'objective lost original block'
        keep = gained
        model = candidate
        history.append(sorted(keep))
    return model, dict(objective_rounds=len(rounds),
        objective_extra_separations=sum(key[0]=='representative' for key in keep-initial), objective_checks=rounds,
        objective_initial_separations=sorted(initial), objective_k_history=history)
