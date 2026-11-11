"""SQLWitness (SIGMOD'26 artifact, Zenodo 10.5281/zenodo.23396996, sqlwitness-code 0.1.0) on one pair.
Settings as in the artifact's experiments/run_sqlwitness.py: termination risk delta=0.05, 60 s per pair.
Request: {tables, constraints, q1, q2, dialect, timeout, delta, seed}."""
import random, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wproto import serve, header_rows_to_instance  # noqa: E402


def handle(r):
    from sqlwitness import counterexample
    random.seed(r.get("seed", 0))
    cons = r["constraints"] or ""
    constraints_path = None
    if r.get("constraints_yml"):  # LeetCode: SQLWitness takes the problem's .yml file path
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False)
        f.write(cons); f.close()
        cons = f.name
        constraints_path = f.name
    try:
        # Align with experiments/run_sqlwitness.py: the released driver passes
        # remove_null/remove_one/remove_literal=False explicitly; the API
        # defaults enable those pruning passes, which is what made a pair that
        # the official driver answers in 0.02-2 s block for the full 60 s.
        found, data, elapsed, iters = counterexample(
            r["tables"], cons, r["q1"], r["q2"], timeout=r["timeout"], dialect=r["dialect"],
            termination_target_risk=r.get("delta", 0.05),
            remove_null=False, remove_one=False, remove_literal=False)
    finally:
        if constraints_path is not None:
            os.unlink(constraints_path)
    if found:
        return {"verdict": "neq", "instance": header_rows_to_instance(data), "secs": elapsed, "iterations": iters}
    v = "timeout" if elapsed >= 0.98 * r["timeout"] else "no_refutation"
    return {"verdict": v, "secs": elapsed, "iterations": iters}


if __name__ == "__main__":
    serve(handle)
