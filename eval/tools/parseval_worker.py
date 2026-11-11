"""ParSEval v1.0.1 `disprove(q1, q2, ddl, url, dialect)` on one pair. The per-pair budget is split over the two
generation runs ParSEval does (time_limit_s = budget/2 each), solver call timeout 3 s (as src/run_parseval.py).
Request: {ddl, q1, q2, dialect (sqlite|mysql|postgres), url, timeout, seed}."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wproto import serve  # noqa: E402

V = {"NEQ": "neq", "EQ": "eq", "TIMEOUT": "timeout", "UNKNOWN": "unsupported", "SYNTAX_ERROR": "unsupported"}


def handle(r):
    from parseval import GenerationConfig, disprove
    res = disprove(r["q1"], r["q2"], r["ddl"], r["url"], r["dialect"],
                   config=GenerationConfig(timeout_ms=3000, time_limit_s=r["timeout"] / 2, seed=r.get("seed", 0)),
                   timeout_s=min(30, r["timeout"]))
    out = {"verdict": V.get(res.verdict.name, res.verdict.name), "reason": (res.reason or "")[:300]}
    if res.counterexample is not None:
        inst = res.counterexample
        cex = {}
        for tb in inst.catalog.tables():
            cols = [b.name.text for b in tb.columns]
            cex[tb.name.parts[-1].text] = [dict(zip(cols, [v if v is None or isinstance(v, (int, float, bool)) else str(v)
                                                           for v in row])) for row in inst.rows(tb.relation)]
        out["instance"] = cex
    return out


if __name__ == "__main__":
    serve(handle)
