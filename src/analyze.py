"""Summarise labels.jsonl (+ parseval.jsonl if present). Re-checks every ParSEval NEQ counterexample:
does it satisfy the declared PK/FK/NOT NULL constraints, and does it still separate the pair in DuckDB?"""
import collections, json, os, statistics as st, sys
import duckdb

sys.path.insert(0, os.path.dirname(__file__))
from bounded import SCHEMA, PK, FK, QUERIES, tiebreak, run, same  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
L = [json.loads(l) for l in open(sys.argv[1])]
PV = [json.loads(l) for l in open(sys.argv[2])] if len(sys.argv) > 2 and os.path.exists(sys.argv[2]) else []
CON = ("sf", "cascade", "synth", "synth_tie", "repair"); UNC = ("u_null", "u_fk", "u_pk", "u_all")

cnt = collections.Counter(r["label"] for r in L)
print("== labels (n=%d)" % len(L), dict(cnt))
valid = len(L) - cnt["error"]
print("valid rewrites:", valid, " eq_only share of valid: %.3f" % (cnt["eq_only_constraints"] / valid),
      " eq_only share of not-refuted-under-constraints: %.3f" % (cnt["eq_only_constraints"] / (cnt["eq_only_constraints"] + cnt["eq_unconstrained"])))
byq = collections.defaultdict(collections.Counter)
for r in L:
    byq[r["qid"]][r["label"]] += 1
print("per query:", {q: dict(c) for q, c in sorted(byq.items())})
print("queries with >=1 eq_only rewrite:", sum(1 for c in byq.values() if c["eq_only_constraints"]))

print("\n== eq_only_constraints: which violation exposes it; DBs needed by the unconstrained random generator")
per_db = []
for r in L:
    if r["label"] != "eq_only_constraints":
        continue
    f = r["families"]
    hits = {k: (f[k]["hit"], f[k]["n"]) for k in UNC if k in f}
    ua = f.get("u_all", {})
    # per-database false-refutation probability of the all-violations generator (geometric estimate)
    if ua.get("hit"):
        per_db.append(1.0 / max(1, ua["n"]))
    print(f"Q{r['qid']}.{r['rid']}", hits, "|", r["strategy"][:70])

print("\n== timing of bounded labelling per candidate (seconds of DB build+exec attributed)")
def secs(r, fams):
    return sum(r["families"][k]["secs"] for k in fams if k in r["families"])
for lab in ("neq", "eq_only_constraints", "eq_unconstrained"):
    xs = [secs(r, CON) for r in L if r["label"] == lab]
    ys = [r["families"][k]["secs_to_hit"] for r in L if r["label"] == lab for k in CON if r["families"].get(k, {}).get("hit")]
    if xs:
        print(lab, "constrained-phase secs median %.2f max %.2f" % (st.median(xs), max(xs)),
              "| secs-to-first-cex median %.3f" % st.median(ys) if ys else "")
xs = [r["families"]["u_all"]["secs_to_hit"] for r in L if r["label"] == "eq_only_constraints" and r["families"].get("u_all", {}).get("hit")]
if xs:
    print("u_all secs-to-false-cex median %.3f max %.3f" % (st.median(xs), max(xs)))


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
    for t, cols in SCHEMA.items():
        ty = {"int": "BIGINT", "dec": "DECIMAL(15,2)", "str": "VARCHAR", "date": "DATE"}
        con.execute(f"CREATE TABLE {t} (" + ", ".join(f"{c} {ty[k]}" for c, k in cols) + ")")
        for r in cex.get(t, []):
            con.execute(f"INSERT INTO {t} VALUES (" + ",".join("?" * len(cols)) + ")", [r.get(c) for c, _ in cols])
    o, oo = tiebreak(QUERIES[str(qid)]); c, co = tiebreak(sql)
    a, _ = run(con, o, 30); b, _ = run(con, c, 30)
    return same(a, b, bool(oo) and bool(co)) is False


if PV:
    sqlof = {(r["qid"], r["rid"]): r["sql"] for r in L}
    print("\n== ParSEval disprove (full DDL vs constraints stripped)")
    tab = collections.defaultdict(collections.Counter)
    false_ref = collections.defaultdict(list)
    labof = {(r["qid"], r["rid"]): r["label"] for r in L}
    for p in PV:
        p["label"] = labof[(p["qid"], p["rid"])]
        tab[(p["mode"], p["label"])][p["verdict"]] += 1
        if p["verdict"] == "NEQ" and p["label"] != "neq":
            ok, why = satisfies(p["cex"] or {})
            dd = duck_differs(p["cex"] or {}, p["qid"], sqlof[(p["qid"], p["rid"])])
            false_ref[p["mode"]].append((p["qid"], p["rid"], p["label"], ok, why, dd))
    for k in sorted(tab):
        print(k, dict(tab[k]))
    for mode, xs in false_ref.items():
        print(f"\n{mode}: NEQ on rewrites our bounded labeller found equivalent under constraints:")
        for x in xs:
            print("  Q%s.%s %s cex_satisfies_constraints=%s %s duckdb_differs=%s" % x)
    # ParSEval timing
    for mode in ("full", "stripped"):
        xs = [p["secs"] for p in PV if p["mode"] == mode and p["secs"] is not None]
        print(mode, "ParSEval secs/candidate median %.1f mean %.1f max %.1f" % (st.median(xs), st.mean(xs), max(xs)))
