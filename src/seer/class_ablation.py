"""Adapters for the existing component interventions in the class schedule."""
from contextlib import contextmanager


@contextmanager
def decision_pool(engine, phase):
    """Hide earlier decision libraries; retain them only as output evidence.

The decision unit is an official quick pair or one class-verifier context.
SAT feedback within that class context remains part of class verification.
Subsequent decisions begin from the seeds again, including after a split.
"""
    if not engine.no_pool_reuse:
        yield
        return
    saved = list(engine.witnesses)
    seeds = [w for w in saved if engine._witness_origin.get(id(w)) == 'seed']
    seed_ids = {id(w) for w in seeds}
    engine.metrics['pool_resets'] += 1
    engine.metrics['decision_pool_size'] = len(seeds)
    engine.events.append({'event': 'pool_decision_reset', 'phase': phase,
                          'seed_columns': len(seeds), 'hidden_columns': len(saved)-len(seeds)})
    try:
        engine.witnesses = seeds
        engine._reindex()
        assert all(engine._witness_origin.get(id(w)) == 'seed' for w in engine.witnesses)
        yield
    finally:
        for w in engine.witnesses:
            if id(w) not in seed_ids and all(w is not old for old in saved):
                saved.append(w)
        engine.witnesses = saved
        engine._reindex()
        engine._recompute_attribution()


def queue_arrival_pairs(engine, q, prior_reps, prior_members):
    if engine.no_fingerprint_grouping:
        targets = prior_reps
    elif engine.no_certificate_transfer:
        targets = prior_members
    else:
        return False
    for rep in targets:
        if rep != q:
            engine._class_arrival_pairs.append((rep, q))
    return True
