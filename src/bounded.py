"""Bounded-testing labeller for LLM rewrites of TPC-H queries (DuckDB).

Families of test databases
  constrained (satisfy TPC-H PK / FK / NOT NULL):
    sf      : full TPC-H SF0.01 (dbgen)
    cascade : random subsets of SF0.01 with cascading deletes of dependants, plus constraint-safe
              non-key value mutations (values from the literal pool)
    synth   : tiny synthetic databases, small key domains, values from SF0.01 pools + query literals
  unconstrained (same synth generator with one class of violation switched on):
    u_null  : NULLs in any column (incl. key columns)
    u_fk    : dangling foreign keys
    u_pk    : duplicate primary keys / duplicate rows
    u_all   : all three

Label per rewrite: error | neq (counterexample on a constrained DB) | eq_only_constraints (no constrained
counterexample, but an unconstrained one) | eq_unconstrained (no counterexample anywhere). All "eq" labels are
bounded-testing evidence, not proofs; "neq" and the unconstrained counterexamples are concrete witnesses.

Usage: python bounded.py REWRITES.jsonl OUT.jsonl [--workers N] [--n-synth N] [--n-cascade N] [--qids 1,2]
"""
import argparse, datetime as dt, decimal, json, os, random, re, sys, threading, time
from multiprocessing import Pool

import duckdb, pyarrow as pa, pyarrow.parquet as pq, sqlglot
from sqlglot import exp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SF = f"{ROOT}/data/sf001"
QUERIES = json.load(open(f"{ROOT}/data/tpch_queries.json"))

# ---------------------------------------------------------------- schema
D = "date"; I = "int"; N = "dec"; S = "str"
SCHEMA = {
    "region": [("r_regionkey", I), ("r_name", S), ("r_comment", S)],
    "nation": [("n_nationkey", I), ("n_name", S), ("n_regionkey", I), ("n_comment", S)],
    "part": [("p_partkey", I), ("p_name", S), ("p_mfgr", S), ("p_brand", S), ("p_type", S), ("p_size", I),
             ("p_container", S), ("p_retailprice", N), ("p_comment", S)],
    "supplier": [("s_suppkey", I), ("s_name", S), ("s_address", S), ("s_nationkey", I), ("s_phone", S),
                 ("s_acctbal", N), ("s_comment", S)],
    "partsupp": [("ps_partkey", I), ("ps_suppkey", I), ("ps_availqty", I), ("ps_supplycost", N), ("ps_comment", S)],
    "customer": [("c_custkey", I), ("c_name", S), ("c_address", S), ("c_nationkey", I), ("c_phone", S),
                 ("c_acctbal", N), ("c_mktsegment", S), ("c_comment", S)],
    "orders": [("o_orderkey", I), ("o_custkey", I), ("o_orderstatus", S), ("o_totalprice", N), ("o_orderdate", D),
               ("o_orderpriority", S), ("o_clerk", S), ("o_shippriority", I), ("o_comment", S)],
    "lineitem": [("l_orderkey", I), ("l_partkey", I), ("l_suppkey", I), ("l_linenumber", I), ("l_quantity", N),
                 ("l_extendedprice", N), ("l_discount", N), ("l_tax", N), ("l_returnflag", S), ("l_linestatus", S),
                 ("l_shipdate", D), ("l_commitdate", D), ("l_receiptdate", D), ("l_shipinstruct", S),
                 ("l_shipmode", S), ("l_comment", S)],
}
PK = {"region": ["r_regionkey"], "nation": ["n_nationkey"], "part": ["p_partkey"], "supplier": ["s_suppkey"],
      "partsupp": ["ps_partkey", "ps_suppkey"], "customer": ["c_custkey"], "orders": ["o_orderkey"],
      "lineitem": ["l_orderkey", "l_linenumber"]}
FK = {"nation": [(["n_regionkey"], "region")], "supplier": [(["s_nationkey"], "nation")],
      "customer": [(["c_nationkey"], "nation")],
      "partsupp": [(["ps_partkey"], "part"), (["ps_suppkey"], "supplier")],
      "orders": [(["o_custkey"], "customer")],
      "lineitem": [(["l_orderkey"], "orders"), (["l_partkey", "l_suppkey"], "partsupp")]}
ORDER = ["region", "nation", "part", "supplier", "partsupp", "customer", "orders", "lineitem"]
KEYDOM = {"r_regionkey": 3, "n_nationkey": 4, "p_partkey": 4, "s_suppkey": 4, "c_custkey": 4, "o_orderkey": 6,
          "l_linenumber": 3}
ROWS = {"region": (1, 3), "nation": (1, 4), "part": (0, 4), "supplier": (0, 4), "partsupp": (0, 7),
        "customer": (0, 4), "orders": (0, 6), "lineitem": (0, 10)}
PA_T = {I: pa.int64(), N: pa.decimal128(15, 2), S: pa.string(), D: pa.date32()}
KEYCOLS = {c for t in PK for c in PK[t]} | {c for t in FK for cols, _ in FK[t] for c in cols}


# ---------------------------------------------------------------- literal pools
def literals(sqls):
    """String / number / date literals appearing in any of the queries, with boundary neighbours."""
    strs, nums, dates = set(), set(), set()
    for sql in sqls:
        try:
            tree = sqlglot.parse_one(sql, read="duckdb")
        except Exception:
            continue
        for lit in tree.find_all(exp.Literal):
            v = lit.this
            if lit.is_string:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    d0 = dt.date.fromisoformat(v)
                    for delta in (0, -1, 1, 30, -30, 31, 90, 91, -90, 365, -365, 366, 89, 92, 364):
                        dates.add(d0 + dt.timedelta(days=delta))
                    for mm in (1, 3, 12):  # month arithmetic boundaries
                        m = d0.month - 1 + mm
                        try:
                            dates.add(d0.replace(year=d0.year + m // 12, month=m % 12 + 1) - dt.timedelta(days=1))
                            dates.add(d0.replace(year=d0.year + m // 12, month=m % 12 + 1))
                        except ValueError:
                            pass
                elif "%" in v or "_" in v:
                    core = v.replace("%", " ").replace("_", "a")
                    strs.update({core.strip(), core, "x" + core + "x", v.replace("%", "")})
                else:
                    strs.add(v)
            else:
                try:
                    x = decimal.Decimal(v)
                except Exception:
                    continue
                for delta in ("0", "1", "-1", "0.01", "-0.01"):
                    nums.add(x + decimal.Decimal(delta))
    return strs, nums, dates


class Pools:
    def __init__(self, sqls, rng):
        self.sf = {t: pq.read_table(f"{SF}/{t}.parquet").to_pylist() for t in ORDER}
        strs, nums, dates = literals(sqls)
        self.col = {}
        for t, cols in SCHEMA.items():
            rows = self.sf[t]
            for c, ty in cols:
                vals = list({r[c] for r in rng.sample(rows, min(len(rows), 400))})
                vals = rng.sample(vals, min(len(vals), 40))
                if ty == S:
                    vals += list(strs)
                elif ty == D:
                    vals += list(dates)
                elif ty in (I, N):
                    lits = [x for x in nums if abs(x) < 10 ** 12]
                    if ty == I:
                        lits = [int(x) for x in lits if x == int(x)]
                    else:
                        lits = [x.quantize(decimal.Decimal("0.01")) for x in lits]
                    vals += lits
                self.col[c] = vals
        self.strs, self.nums, self.dates = strs, nums, dates


# ---------------------------------------------------------------- database generators
class _Tie:
    def __init__(self, pools, rng, k=3):
        self.col = {c: rng.sample(v, min(k, len(v))) for c, v in pools.col.items()}


def synth(pools, rng, null=False, fk=False, pk=False, tie=False):
    if tie:
        pools = _Tie(pools, rng)
    db = {}
    for t in ORDER:
        lo, hi = ROWS[t]
        n = rng.randint(lo, hi)
        cols = SCHEMA[t]
        rows, seen = [], set()
        for _ in range(n * 3):
            if len(rows) >= n:
                break
            r = {}
            for c, ty in cols:
                r[c] = rng.choice(pools.col[c]) if pools.col[c] else None
            # key columns
            for c in PK[t]:
                if c in KEYDOM:
                    r[c] = rng.randint(1 if c != "r_regionkey" and c != "n_nationkey" else 0, KEYDOM[c])
            for fcols, parent in FK.get(t, []):
                parents = db[parent]
                pcols = PK[parent]
                if fk and rng.random() < 0.4:
                    for c, pc in zip(fcols, pcols):
                        r[c] = rng.randint(0, KEYDOM.get(pc, 4) + 1)
                elif parents:
                    p = rng.choice(parents)
                    for c, pc in zip(fcols, pcols):
                        r[c] = p[pc]
                else:
                    r = None
                    break
            if r is None:
                continue
            if t == "partsupp" and not fk:
                pass
            key = tuple(r[c] for c in PK[t])
            if key in seen and not (pk and rng.random() < 0.7):
                continue
            seen.add(key)
            if null:
                for c, _ in cols:
                    if rng.random() < (0.1 if c in KEYCOLS else 0.15):
                        r[c] = None
            rows.append(r)
            if pk and rng.random() < 0.15:
                rows.append(dict(r))  # exact duplicate row
        db[t] = rows
    return db


def cascade(pools, rng):
    """Random subset of SF0.01 with cascading deletes, plus non-key value mutations from the pool."""
    sf = pools.sf
    p = rng.choice([0.002, 0.005, 0.01, 0.03, 0.1, 0.3])
    db = {}
    for t in ORDER:
        keep_p = 1.0 if t in ("region", "nation") and rng.random() < 0.5 else p * rng.choice([1, 3, 10])
        rows = [r for r in sf[t] if rng.random() < min(1.0, keep_p)]
        for fcols, parent in FK.get(t, []):
            keys = {tuple(r[c] for c in PK[parent]) for r in db[parent]}
            rows = [r for r in rows if tuple(r[c] for c in fcols) in keys]
        rows = [dict(r) for r in rows]
        mut = rng.choice([0, 0.05, 0.2])
        for r in rows:
            if rng.random() < mut:
                c, ty = rng.choice([x for x in SCHEMA[t] if x[0] not in KEYCOLS])
                r[c] = rng.choice(pools.col[c])
        db[t] = rows
    return db


def full_sf(pools, rng):
    return {t: pools.sf[t] for t in ORDER}


def denorm(db):
    out = {}
    for t, cols in SCHEMA.items():
        ty = dict(cols)
        rows = []
        for r in db.get(t, []):
            x = {}
            for c, v in r.items():
                if v is None or v == "NaN":
                    x[c] = None
                elif ty[c] == D:
                    x[c] = dt.date.fromisoformat(v)
                elif ty[c] == N:
                    x[c] = decimal.Decimal(str(v)).quantize(decimal.Decimal("0.01"))
                elif ty[c] == I:
                    x[c] = int(v)
                else:
                    x[c] = v
            rows.append(x)
        out[t] = rows
    return out


def repair(pools, rng, cexs):
    """Constraint-respecting repair of a constraint-violating counterexample: NULLs -> pool values, duplicate
    keys -> drop or renumber, dangling foreign keys -> redirect, drop (cascade) or insert the missing parent."""
    for _ in range(20):
        db = {t: [dict(r) for r in rows] for t, rows in denorm(rng.choice(cexs)).items()}
        for _it in range(6):
            for t in ORDER:
                cols = SCHEMA[t]
                for r in db[t]:
                    for c, _ty in cols:
                        if r[c] is None:
                            r[c] = rng.randint(0, KEYDOM.get(c, 4)) if c in KEYCOLS else rng.choice(pools.col[c])
                for fcols, parent in FK.get(t, []):
                    pks = [tuple(p[c] for c in PK[parent]) for p in db[parent]]
                    keep = []
                    for r in db[t]:
                        k = tuple(r[c] for c in fcols)
                        if k in pks:
                            keep.append(r); continue
                        act = rng.random()
                        if act < 0.4 and pks:
                            for c, v in zip(fcols, rng.choice(pks)):
                                r[c] = v
                            keep.append(r)
                        elif act < 0.7:
                            continue  # drop (cascade)
                        else:
                            new = {c: rng.choice(pools.col[c]) for c, _ in SCHEMA[parent]}
                            for c, v in zip(PK[parent], k):
                                new[c] = v
                            db[parent].append(new); pks.append(k)
                            keep.append(r)
                    db[t] = keep
                seen, keep = set(), []
                for r in db[t]:
                    k = tuple(r[c] for c in PK[t])
                    if k in seen:
                        if rng.random() < 0.5:
                            continue
                        free = [c for c in PK[t] if c in KEYDOM]
                        if not free:
                            continue
                        r[free[0]] = max(x[free[0]] for x in db[t]) + rng.randint(1, 3)
                        k = tuple(r[c] for c in PK[t])
                    seen.add(k); keep.append(r)
                db[t] = keep
            if check_constraints(db):
                return db
    return {t: [] for t in ORDER}


def to_arrow(db):
    out = {}
    for t, cols in SCHEMA.items():
        sch = pa.schema([(c, PA_T[ty]) for c, ty in cols])
        out[t] = pa.Table.from_pylist(db[t], schema=sch)
    return out


def check_constraints(db):
    """Assertion helper: True iff db satisfies PK, FK and NOT NULL."""
    for t, cols in SCHEMA.items():
        keys = set()
        for r in db[t]:
            if any(r[c] is None for c, _ in cols):
                return False
            k = tuple(r[c] for c in PK[t])
            if k in keys:
                return False
            keys.add(k)
        for fcols, parent in FK.get(t, []):
            pk = {tuple(r[c] for c in PK[parent]) for r in db[parent]}
            if any(tuple(r[c] for c in fcols) not in pk for r in db[t]):
                return False
    return True


# ---------------------------------------------------------------- execution & comparison
def tiebreak(sql):
    """If the outermost query is ordered, append positional tie-breakers on every output column."""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return sql, None
    order = tree.args.get("order")
    if not order:
        return sql, False
    n = len(tree.selects) if hasattr(tree, "selects") and tree.selects else 0
    if n == 0 or any(isinstance(s, exp.Star) for s in tree.selects):
        return sql, True
    for i in range(1, n + 1):
        order.append("expressions", exp.Ordered(this=exp.Literal.number(i)))
    return tree.sql(dialect="duckdb"), True


def norm(v):
    if isinstance(v, (float, decimal.Decimal)):
        f = float(v)
        if f != f:
            return "NaN"
        return float(f"{f:.9g}")
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, str):
        return v.rstrip()
    return v


def run(con, sql, timeout):
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    t = time.perf_counter()
    try:
        rows = con.execute(sql).fetchall()
        return [tuple(norm(v) for v in r) for r in rows], time.perf_counter() - t
    except Exception as e:
        return "ERR:" + type(e).__name__ + ":" + str(e)[:160], time.perf_counter() - t
    finally:
        timer.cancel()


def same(a, b, ordered):
    if isinstance(a, str) or isinstance(b, str):
        return None
    if len(a) != len(b):
        return False
    if ordered:
        return a == b
    key = lambda r: tuple((x is None, str(type(x)), x if x is not None else 0) for x in r)
    return sorted(a, key=key) == sorted(b, key=key)


GENS = {"sf": (full_sf, {}), "cascade": (cascade, {}), "synth": (synth, {}), "synth_tie": (synth, {"tie": True}),
        "u_null": (synth, {"null": True}), "u_fk": (synth, {"fk": True}), "u_pk": (synth, {"pk": True}),
        "u_all": (synth, {"null": True, "fk": True, "pk": True}), "repair": (None, {})}
CONSTRAINED = ("sf", "cascade", "synth", "synth_tie", "repair")


def task(args):
    """Run original + candidate rewrites of one query on n databases of one family."""
    qid, family, seed, n, cands = args[:5]
    cexs = args[5] if len(args) > 5 else None
    rng = random.Random(f"{qid}-{family}-{seed}")
    orig = QUERIES[str(qid)]
    pools = Pools([orig] + [c["sql"] for c in cands], random.Random(f"pool-{qid}"))
    o_sql, o_ord = tiebreak(orig)
    prepared = []
    for c in cands:
        s, ordd = tiebreak(c["sql"])
        prepared.append((c["rid"], s, bool(o_ord) and bool(ordd)))
    gen, kw = GENS[family]
    con = duckdb.connect(config={"threads": 1})
    res = {c["rid"]: {"first": None, "secs": 0.0, "n": 0, "err": None, "orig_err": None, "cex": None} for c in cands}
    timeout = 60 if family == "sf" else 10
    for i in range(n):
        t0 = time.perf_counter()
        db = repair(pools, rng, cexs) if family == "repair" else gen(pools, rng, **kw)
        if family in CONSTRAINED:
            assert family == "sf" or check_constraints(db), f"constraint violation in {family}"
        tabs = to_arrow(db)
        for t, tab in tabs.items():
            con.register(t, tab)
        build = time.perf_counter() - t0
        ores, osecs = run(con, o_sql, timeout)
        share = (build + osecs) / max(1, len(prepared))
        for rid, s, ordd in prepared:
            r = res[rid]
            if r["first"] is not None:
                continue
            cres, csecs = run(con, s, timeout)
            r["secs"] += share + csecs
            r["n"] += 1
            if isinstance(cres, str):
                if r["err"] is None:
                    r["err"] = cres
                continue
            if isinstance(ores, str):
                r["orig_err"] = ores
                continue
            if not same(ores, cres, ordd):
                r["first"] = i
                if family != "sf" and sum(len(v) for v in db.values()) <= 60:
                    r["cex"] = {t: [{k: norm(v) for k, v in row.items()} for row in rows] for t, rows in db.items()}
                r["cex_sizes"] = {t: len(v) for t, v in db.items()}
                r["orig_out"] = str(ores[:5]); r["cand_out"] = str(cres[:5])
        if all(r["first"] is not None for r in res.values()):
            break
    con.close()
    return qid, family, seed, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rewrites"); ap.add_argument("out")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--n-synth", type=int, default=2000)
    ap.add_argument("--n-cascade", type=int, default=200)
    ap.add_argument("--chunk", type=int, default=250)
    ap.add_argument("--n-repair", type=int, default=3000)
    ap.add_argument("--qids", default="")
    a = ap.parse_args()
    rws = [json.loads(l) for l in open(a.rewrites)]
    if a.qids:
        keep = {int(x) for x in a.qids.split(",")}
        rws = [r for r in rws if r["qid"] in keep]
    byq = {}
    for r in rws:
        byq.setdefault(r["qid"], []).append(r)

    def tasks(fams, alive):
        out = []
        if fams == ("repair",):
            for q, cs in byq.items():
                for c in cs:
                    if (q, c["rid"]) not in alive:
                        continue
                    cexs = [x for f in ("u_null", "u_fk", "u_pk", "u_all") for x in agg[(q, c["rid"])].get(f, {}).get("all_cex", [])]
                    if cexs:
                        for s0 in range(0, a.n_repair, a.chunk):
                            out.append((q, "repair", s0, min(a.chunk, a.n_repair - s0), [c], cexs))
            return out
        for q, cs in byq.items():
            cs = [c for c in cs if (q, c["rid"]) in alive]
            if not cs:
                continue
            for f in fams:
                total = 1 if f == "sf" else (a.n_cascade if f == "cascade" else a.n_synth)
                if f == "synth_tie":
                    total = a.n_synth
                for s in range(0, total, a.chunk):
                    out.append((q, f, s, min(a.chunk, total - s), cs))
        return out

    agg = {}  # (qid, rid) -> family -> merged
    def merge(results):
        for q, f, seed, res in results:
            for rid, r in res.items():
                m = agg.setdefault((q, rid), {}).setdefault(f, {"hit": False, "secs_to_hit": None, "secs": 0.0,
                                                                  "n": 0, "err": None, "orig_err": None, "cex": None})
                m["n"] += r["n"]; m["secs"] += r["secs"]
                m["err"] = m["err"] or r["err"]; m["orig_err"] = m["orig_err"] or r["orig_err"]
                if r["first"] is not None and r.get("cex"):
                    m.setdefault("all_cex", []).append(r["cex"])
                if r["first"] is not None and (not m["hit"] or seed < m.get("seed", 1e9)):
                    m.update(hit=True, seed=seed, cex=r.get("cex"), cex_sizes=r.get("cex_sizes"),
                             orig_out=r.get("orig_out"), cand_out=r.get("cand_out"), secs_to_hit=r["secs"])

    t0 = time.time()
    alive = {(r["qid"], r["rid"]) for r in rws}
    with Pool(a.workers) as pool:
        merge(pool.imap_unordered(task, tasks(("sf", "cascade", "synth", "synth_tie"), alive)))
        print(f"constrained phase done {time.time() - t0:.0f}s", flush=True)
        alive = {k for k, fams in agg.items() if not any(fams[f]["hit"] for f in CONSTRAINED if f in fams)}
        merge(pool.imap_unordered(task, tasks(("u_null", "u_fk", "u_pk", "u_all"), alive)))
        print(f"unconstrained phase done {time.time() - t0:.0f}s", flush=True)
        alive = {k for k, fams in agg.items() if not any(fams[f]["hit"] for f in CONSTRAINED if f in fams)
                 and any(fams.get(f, {}).get("hit") for f in ("u_null", "u_fk", "u_pk", "u_all"))}
        merge(pool.imap_unordered(task, tasks(("repair",), alive)))
        print(f"repair phase done {time.time() - t0:.0f}s", flush=True)

    with open(a.out, "w") as f:
        for r in rws:
            fams = agg.get((r["qid"], r["rid"]), {})
            for v in fams.values():
                v.pop("all_cex", None)
            errs = [fams[x]["err"] for x in fams if fams[x]["err"] and fams[x]["n"] and not fams[x]["hit"]]
            ran_ok = any(fams[x]["hit"] or (fams[x]["n"] and not fams[x]["err"]) for x in fams)
            if any(fams.get(x, {}).get("hit") for x in CONSTRAINED):
                label = "neq"
            elif not ran_ok:
                label = "error"
            elif any(fams.get(x, {}).get("hit") for x in ("u_null", "u_fk", "u_pk", "u_all")):
                label = "eq_only_constraints"
            else:
                label = "eq_unconstrained"
            f.write(json.dumps({**r, "label": label, "families": fams, "errors": errs[:1]}, default=str) + "\n")
    print("wrote", a.out, f"{time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
