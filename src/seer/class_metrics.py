"""Summarize the original block receipts without rewriting raw evidence.

Block queries are class verifier requests (the frozen class_checks counter),
not pair-decision requests or individual Z3 checks. Unsupported requests and
pre-check guards remain explicit, and are never called UNSAT or a timeout.
"""
from collections import defaultdict


def depth_counts(events, expected_checks=None):
    counts = defaultdict(lambda: dict(block_queries=0, sat=0, unsat=0,
        timeout=0, unknown=0, unsupported=0, other=0, chunk_reductions=0,
        solver_calls_reported=0, solver_calls_incomplete=False))
    for event in events:
        if event.get('event') != 'class_bounded_search':
            continue
        r = event['receipt']; k = r['k']
        assert isinstance(k, int) and k > 0
        c = counts[str(k)]
        if r['status'] == 'reduce_chunk':
            c['chunk_reductions'] += 1
            continue
        c['block_queries'] += 1
        c['solver_calls_reported'] += r.get('solver_calls', 0)
        if 'objective_rounds' in r:
            c['objective_rounds'] = c.get('objective_rounds', 0) + r['objective_rounds']
            c['objective_extra_separations'] = c.get('objective_extra_separations', 0) + r['objective_extra_separations']
            for check in r['objective_checks']:
                result = check['result']
                key = 'objective_' + ('timeout' if result == 'unknown' and
                    check.get('unknown_reason') in ('timeout','deadline') else result)
                c[key] = c.get(key, 0) + 1
        reason = r.get('reason', '')
        if reason in ('class_hard_timeout', 'TimeoutError') or r.get('solver_unknown_reason') in ('timeout', 'deadline'):
            category = 'timeout'
            # The old hard-deadline worker cannot return its interrupted
            # low-level count. Entry instrumentation remains authoritative.
            c['solver_calls_incomplete'] |= reason == 'class_hard_timeout'
        elif reason == 'solver_unknown':
            category = 'unknown'
        elif r['status'] == 'candidate':
            category = 'sat'
        elif r['status'] == 'domain_unsat' or reason == 'error_or_nondeterminism_scope_not_closed':
            # This latter reason is reachable only after the *difference*
            # disjunction is UNSAT; safety-guard admission is separate.
            category = 'unsat'
        elif r['status'] == 'unsupported':
            category = 'unsupported'
        else:
            category = 'other'
        c[category] += 1
    result = dict(sorted(counts.items(), key=lambda item: int(item[0])))
    for c in result.values():
        assert c['block_queries'] == sum(c[k] for k in
            ('sat', 'unsat', 'timeout', 'unknown', 'unsupported', 'other'))
    if expected_checks is not None:
        assert sum(c['block_queries'] for c in result.values()) == expected_checks
    return result
