"""VeriEQL v1.0 (github.com/VeriEQL/VeriEQL, commit 3b99928), unmodified, author z3py files installed; reference
baseline at bound 2 (the SQLWitness paper setting). Its result is bounded equivalence or a counterexample.
Known input semantics (recorded, not repaired): VeriEQL upper-cases whole queries, string literals included, so
counterexamples for case-sensitive string predicates can be spurious; the shared validator classifies them.
Request: {tables: {T: {C: TYPE}}, constraints: [...], q1, q2, timeout, bound, dialect}."""
import logging, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wproto import serve  # noqa: E402


def _val(v):
    if v is None or v == "NULL":
        return None
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1]
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def handle(r):
    from environment import Environment
    from constants import DIALECT
    from logger import LOGGER
    LOGGER.setLevel(logging.ERROR)
    d = {"mysql": DIALECT.MYSQL, "postgresql": DIALECT.POSTGRESQL}.get(r["dialect"], DIALECT.ALL)
    with Environment(generate_code=True, show_counterexample=True, timer=True, dialect=d) as env:
        env.solver.set(timeout=int(r["timeout"] * 1000))
        for t, cols in r["tables"].items():
            env.create_database(attributes=cols, bound_size=r.get("bound", 2), name=t)
        env.add_constraints(r["constraints"] or None)
        env.save_checkpoints()
        res = env.analyze(r["q1"], r["q2"])
        out = {"secs": (env.traversing_time or 0) + (env.solving_time or 0)}
        if res is True:
            return {**out, "verdict": "eq"}
        if res is False:
            inst = {t: [{c.split("__", 1)[-1]: _val(v) for c, v in row.items()} for row in rows]
                    for t, rows in env.counterexample_dict.items()}
            return {**out, "verdict": "neq", "instance": inst}
        return {**out, "verdict": "timeout" if str(res).upper() in ("UNK", "UNKNOWN", "NONE") else "unsupported",
                "reason": str(res)[:200]}


serve(handle)
