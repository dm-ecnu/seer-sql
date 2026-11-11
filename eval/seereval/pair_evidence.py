"""Offline pair evidence using the unchanged shared comparison contract.

Equal observations leave a pair unresolved; only an accepted genuine database
is evidence of separation. No partition or transitivity assumption is used.
"""
from __future__ import annotations
from collections import Counter
from .validator import classify_from_check


def pair_evidence(pairs, indexed_checks):
    pairs = [tuple(p) for p in pairs]
    assert all(len(p) == 2 and p[0] != p[1] for p in pairs), "distinct endpoints required"
    keys = [frozenset(p) for p in pairs]
    assert len(set(keys)) == len(keys), "duplicate comparison pair"
    evidence = {key: {"a": pair[0], "b": pair[1], "certificate": None,
                      "same_observations": 0, "execution_failures": 0}
                for key, pair in zip(keys, pairs)}
    seen, statuses = set(), Counter()
    for index, checked in indexed_checks:
        assert index not in seen, "duplicate certificate index"
        seen.add(index)
        status = checked["status"]
        assert status in ("valid", "violating", "load_error"), status
        statuses[status] += 1
        if status != "valid":
            continue
        for key, (a, b) in zip(keys, pairs):
            outcome = classify_from_check(checked, a, b)["class"]
            row = evidence[key]
            if outcome == "genuine":
                if row["certificate"] is None:
                    row["certificate"] = index
            elif outcome == "spurious":
                row["same_observations"] += 1
            else:
                assert outcome == "exec_error", outcome
                row["execution_failures"] += 1
    records = list(evidence.values())
    for row in records:
        row["verdict"] = "split" if row["certificate"] is not None else "undecided"
    return {"pairs": records, "checked_database_statuses": dict(statuses),
            "n_genuine_pairs": sum(r["verdict"] == "split" for r in records),
            "n_databases": len(seen)}
