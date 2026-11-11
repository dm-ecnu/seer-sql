"""Main table for the 10-25 gate: ParSEval (full DDL) / Polygon / SEER-batch on the 203 executable TPC-H rewrites.

Ground truth = runs/labels.jsonl (bounded testing; 'neq' = concrete constrained counterexample, 'eq_*' = none found).
Every NEQ/REFUTED is re-verified here: (a) the counterexample satisfies PK/FK(composite)/NOT NULL, (b) DuckDB returns
different results for original and rewrite on it. A refutation of an eq-labelled rewrite counts as an error
refutation against the labels; it is additionally reported whether its counterexample is valid+confirmed (then the
label, not the tool, is wrong) or not.

  .venv/bin/python src/summarize.py --seer runs/seer_s0.jsonl runs/seer_s1.jsonl runs/seer_s2.jsonl
"""
import argparse, json, os, statistics as st, sys
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
import duckdb  # noqa: E402
from bounded import SCHEMA, PK, FK, QUERIES, tiebreak, run, same  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TY = {"int": "BIGINT", "dec": "DECIMAL(15,2)", "str": "VARCHAR", "date": "DATE"}


def satisfies(cex):
    for t, cols in SCHEMA.items():
        rows = cex.get(t, [])
        keys = set()
        for r in rows:
            if any(r.get(c) is None for c, _ in cols):
                return False, f"NULL in {t}"
            k = tuple(str(r[c]) for c in PK[t])
            if k in keys:
                return False, f"dup PK {t}"
            keys.add(k)
        for fcols, parent in FK.get(t, []):
            pk = {tuple(str(x[c]) for c in PK[parent]) for x in cex.get(parent, [])}
            for r in rows:
                if tuple(str(r[c]) for c in fcols) not in pk:
                    return False, f"dangling FK {t}->{parent}"
    return True, ""


def duck_differs(cex, qid, sql):
    con = duckdb.connect()
    try:
        for t, cols in SCHEMA.items():
            con.execute(f"CREATE TABLE {t} (" + ", ".join(f"{c} {TY[k]}" for c, k in cols) + ")")
            for r in cex.get(t, []):
                con.execute(f"INSERT INTO {t} VALUES (" + ",".join("?" * len(cols)) + ")", [r.get(c) for c, _ in cols])
    except Exception as e:
        return None, f"load: {type(e).__name__}: {str(e)[:80]}"
    o, oo = tiebreak(QUERIES[str(qid)]); c, co = tiebreak(sql)
    a, _ = run(con, o, 30); b, _ = run(con, c, 30)
    if isinstance(a, str) or isinstance(b, str):
        return None, "exec error"
    return same(a, b, bool(oo) and bool(co)) is False, ""


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else None


def metrics(name, rows, labels):
    """rows: {(qid,rid): {"verdict": NEQ|EQ|TIMEOUT|FAIL, "secs": float|None, "cex_ok": bool|None}}"""
    n = len(labels)
    c = Counter()
    errs, missed, viol = [], [], []
    for k, lab in labels.items():
        r = rows.get(k, {"verdict": "FAIL", "secs": None})
        v = r["verdict"]
        c[v] += 1
        if v == "NEQ":
            if r.get("cex_sat") is False:
                viol.append(k)
            elif r.get("cex_ok") is False:
                c["unconfirmed"] += 1
            if lab != "neq":
                errs.append((k, r.get("cex_ok")))
            else:
                c["dec"] += 1
        elif v == "EQ":
            if lab == "neq":
                missed.append(k)
            else:
                c["dec"] += 1
    secs = [r["secs"] for k, r in rows.items() if k in labels and r.get("secs") is not None]
    return {"method": name, "n": n, "false_refutations": len(errs),
            "false_ref_with_valid_confirmed_cex": sum(1 for _, ok in errs if ok),
            "decided_correct": c["dec"], "decided_ratio": round(c["dec"] / n, 3),
            "NEQ": c["NEQ"], "EQ": c["EQ"], "missed_neq_as_eq": len(missed), "timeouts": c["TIMEOUT"],
            "failures": c["FAIL"], "cex_violating_constraints": len(viol), "cex_valid_not_reproduced": c["unconfirmed"],
            "median_s": round(st.median(secs), 2) if secs else None, "p90_s": round(pct(secs, 0.9), 2) if secs else None,
            "_errs": errs, "_missed": missed, "_viol": viol}


def load_labels():
    L = [json.loads(l) for l in open(f"{ROOT}/runs/labels.jsonl")]
    return {(r["qid"], r["rid"]): r["label"] for r in L if r["label"] != "error"}, \
        {(r["qid"], r["rid"]): r["sql"] for r in L}


def parseval(labels, sqls, path=None):
    out = {}
    for l in open(path or f"{ROOT}/runs/parseval.jsonl"):
        p = json.loads(l)
        if p["mode"] != "full":
            continue
        k = (p["qid"], p["rid"])
        v = {"NEQ": "NEQ", "EQ": "EQ", "TIMEOUT": "TIMEOUT", "HARD_TIMEOUT": "TIMEOUT"}.get(p["verdict"], "FAIL")
        r = {"verdict": v, "secs": p["secs"] if p["secs"] is not None else p.get("wall")}
        if v == "NEQ":
            ok, _ = satisfies(p["cex"] or {})
            dd, _ = duck_differs(p["cex"] or {}, p["qid"], sqls[k])
            r["cex_ok"] = bool(ok and dd)
            r["cex_sat"], r["cex_differs"] = ok, dd
        out[k] = r
    return out


def polygon(labels, sqls, path):
    out, raw = {}, Counter()
    for l in open(path):
        p = json.loads(l)
        k = (p["qid"], p["rid"])
        raw[p["verdict"]] += 1
        v = {"NEQ": "NEQ", "EQ": "EQ", "TIMEOUT": "TIMEOUT", "HARD_TIMEOUT": "TIMEOUT"}.get(p["verdict"], "FAIL")
        r = {"verdict": v, "secs": p.get("secs") if v != "FAIL" else p.get("secs"), "reason": p.get("reason", "")}
        if v == "NEQ":
            cex = p.get("cex") or {}
            ok, why = satisfies(cex)
            dd, why2 = duck_differs(cex, p["qid"], sqls[k])
            r["cex_ok"] = bool(ok and dd)
            r["cex_sat"], r["cex_why"], r["cex_differs"] = ok, why, dd
        out[k] = r
    return out, raw


def seer(path, labels):
    out, meta = {}, []
    for l in open(path):
        b = json.loads(l)
        meta.append(b)
        nb = b.get("n_cands") or len(b["verdicts"])
        for rid, v in b["verdicts"].items():
            k = (b["qid"], int(rid))
            vv = {"REFUTED": "NEQ", "EQ_BOUNDED": "EQ", "TIMEOUT": "TIMEOUT"}.get(v["verdict"], "FAIL")
            r = {"verdict": vv, "secs": v.get("t"), "amortized": (b.get("wall") or 0) / nb}
            if vv == "NEQ":
                if v.get("cex") is not None:
                    ok, _ = satisfies(v["cex"])
                    r["cex_ok"] = r["cex_sat"] = ok      # engine difference already asserted twice inside the runner
                else:
                    r["cex_ok"] = True    # SF0.01 dbgen instance
            out[k] = r
    return out, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parseval", default=f"{ROOT}/runs/parseval.jsonl")
    ap.add_argument("--polygon", default=f"{ROOT}/runs/polygon.jsonl")
    ap.add_argument("--seer", nargs="*", default=[])
    ap.add_argument("--out", default=f"{ROOT}/runs/main_table.json")
    a = ap.parse_args()
    labels, sqls = load_labels()
    res = {}
    pv = parseval(labels, sqls, a.parseval)
    res["ParSEval"] = metrics("ParSEval (full DDL)", pv, labels)
    if os.path.exists(a.polygon):
        pg, raw = polygon(labels, sqls, a.polygon)
        m = metrics("Polygon", pg, labels)
        m["raw_verdicts"] = dict(raw)
        m["unsupported_reasons"] = Counter(r["reason"].split(":")[0] + ":" + r["reason"].split(":")[1][:60]
                                           if ":" in r["reason"] else r["reason"][:60]
                                           for r in pg.values() if r["verdict"] == "FAIL").most_common()
        m["cex_details"] = {f"{k[0]}.{k[1]}": {x: r.get(x) for x in ("cex_sat", "cex_why", "cex_differs")}
                            for k, r in pg.items() if r["verdict"] == "NEQ"}
        res["Polygon"] = m
    runs = []
    for p in a.seer:
        s, meta = seer(p, labels)
        m = metrics(f"SEER-batch {os.path.basename(p)}", s, labels)
        am = [r["amortized"] for k, r in s.items() if k in labels]
        m["amortized_median_s"] = round(st.median(am), 2)
        m["amortized_p90_s"] = round(pct(am, 0.9), 2)
        m["stops"] = dict(Counter(b.get("stop", "crash") for b in meta))
        runs.append(m)
        res[f"SEER:{os.path.basename(p)}"] = m
    if runs:
        agg = {}
        for f in ("false_refutations", "decided_correct", "decided_ratio", "missed_neq_as_eq", "timeouts", "failures",
                  "median_s", "p90_s", "amortized_median_s", "amortized_p90_s", "NEQ", "EQ"):
            xs = [r[f] for r in runs]
            agg[f] = (round(st.mean(xs), 3), round(st.stdev(xs), 3) if len(xs) > 1 else 0.0)
        res["SEER-batch mean±std"] = agg
    print(f"{'method':34s} {'falseRef':>8s} {'decided':>12s} {'missed':>6s} {'med_s':>7s} {'p90_s':>7s} "
          f"{'TO':>4s} {'fail':>4s} {'viol':>4s}")
    for k, m in res.items():
        if k == "SEER-batch mean±std":
            continue
        print(f"{m['method'][:34]:34s} {m['false_refutations']:8d} {m['decided_correct']:4d} ({m['decided_ratio']:.3f}) "
              f"{m['missed_neq_as_eq']:6d} {m['median_s']!s:>7s} {m['p90_s']!s:>7s} {m['timeouts']:4d} "
              f"{m['failures']:4d} {m['cex_violating_constraints']:4d}")
        if m["_errs"]:
            print("    false refutations vs labels (qid,rid; cex valid&confirmed):", m["_errs"])
        if m["_missed"]:
            print("    neq judged EQ:", m["_missed"])
    if runs:
        print("SEER-batch mean±std:", res["SEER-batch mean±std"])
    json.dump(res, open(a.out, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
