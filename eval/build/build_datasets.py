"""Convert every dataset to the common batch format.

Batch line: {"batch_id", "dataset", "dialect", "schema_path", "queries": [{"qid", "sql", "role"}], "meta": {...}}
`schema_path` is relative to the directory of the jsonl file and points to a normalized schema JSON
(seereval/schema.py) that also keeps the tool-native input (SQLWitness/Polygon schema + constraint text).

  python eval/build/build_datasets.py --raw RAW --out OUT [--only spider,bird,...]
    RAW/sqlwitness/x/SQLWitness-artifact   Zenodo 10.5281/zenodo.23396996 (unzipped)
    RAW/polygon                            Polygon artifact (benchmarks/cubes)
    RAW/xdatan/src                         gitlab.com/xdata/xdatapublic @a860311 (benchmarks/XData-BM, TPCH)
OUT gets <dataset>_pairs.jsonl / <dataset>_batches.jsonl / cubes_d50.jsonl / ..., schemas/<dataset>/*.json and
build_report.json (counts, parse failures, batch-size distributions).
"""
import argparse, csv, json, os, re, sys
from collections import Counter, defaultdict

EVAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(EVAL)
sys.path.insert(0, EVAL)
from seereval.schema import from_sqlwitness, from_ddl, Schema  # noqa: E402

REPORT = {}


def norm_sql(s):
    return re.sub(r"\s+", " ", s.strip().rstrip(";")).lower()


def write_schema(out, ds, name, s: Schema):
    os.makedirs(f"{out}/schemas/{ds}", exist_ok=True)
    rel = f"schemas/{ds}/{name}.json"
    s.dump(f"{out}/{rel}")
    return rel


def dist(sizes):
    c = Counter(sizes)
    return {"n": len(sizes), "queries": sum(sizes), "min": min(sizes), "max": max(sizes),
            "mean": round(sum(sizes) / len(sizes), 2), "hist": dict(sorted(c.items())[:15])}


# ------------------------------------------------------------------ SQLWitness benchmarks
SW = {
    "spider": dict(gt="spider/dev_gold.sql", cand="spider/omni_maj.json", schemas="spider/schemas",
                   cons="spider/constraints", delim=None, dialect="sqlite", valid="spider_invalid.csv"),
    "bird": dict(gt="bird/dev.sql", cand="bird/omni_maj.json", schemas="bird/schemas", cons="bird/constraints",
                 delim="\t", dialect="sqlite", valid="bird_invalid.csv"),
    "minidev_pg": dict(gt="minidev/mini_dev_postgresql_gold.sql", cand="minidev/predict_mini_dev_gpt-5.4_cot_PostgreSQL.json",
                       schemas="minidev/postgresql_schemas", cons="minidev/postgresql_constraints", delim="\t",
                       dialect="postgresql", valid="minidev_postgresql_invalid.csv"),
}


def sw_schema(art, cfg, ds, name, out, cache, rep):
    if name in cache:
        return cache[name]
    p = f"{art}/benchmarks/{cfg['schemas']}/{name}.json"
    if not os.path.exists(p):
        cache[name] = None
        return None
    tables = json.load(open(p)).get("Tables", [])
    cp = f"{art}/benchmarks/{cfg['cons']}/{name}"
    text = open(cp).read().strip() if os.path.exists(cp) else ""
    try:
        s = from_sqlwitness(name, tables, text, source=f"SQLWitness artifact {cfg['schemas']}/{name}.json")
    except Exception as e:  # constraint text SQLWitness itself cannot parse
        rep["constraint_parse_fail"].append(f"{name}: {type(e).__name__}: {str(e)[:100]}")
        s = from_sqlwitness(name, tables, "", source=f"SQLWitness artifact {cfg['schemas']}/{name}.json")
        s.native["sqlwitness"]["constraints"] = text
        s.unsupported.append("UNPARSED: " + text[:500])
    cache[name] = write_schema(out, ds, name, s)
    return cache[name]


def build_sqlwitness(raw, out, ds):
    art = f"{raw}/sqlwitness/x/SQLWitness-artifact"
    cfg = SW[ds]
    gt = [l.strip() for l in open(f"{art}/benchmarks/{cfg['gt']}") if l.strip()]
    cand = [str(q).strip() for q in json.load(open(f"{art}/benchmarks/{cfg['cand']}"))]
    assert len(gt) == len(cand)
    valid = {int(r["Index"]): r["Both_Valid"].strip() == "True"
             for r in csv.DictReader(open(f"{art}/results/paper/sanity_check/{cfg['valid']}"))}
    rep = {"constraint_parse_fail": [], "raw_pairs": len(gt)}
    cache = {}
    pairs = []
    for i, (g, c) in enumerate(zip(gt, cand), start=1):
        q, db = (x.strip() for x in g.rsplit(cfg["delim"], 1))
        if not valid.get(i):
            continue
        sp = sw_schema(art, cfg, ds, db, out, cache, rep)
        if sp is None:
            rep.setdefault("schema_missing", []).append(i)
            continue
        pairs.append({"batch_id": f"{ds}-p{i:04d}", "dataset": f"{ds}_pairs", "dialect": cfg["dialect"],
                      "schema_path": sp,
                      "queries": [{"qid": "gold", "sql": q, "role": "reference"},
                                  {"qid": "cand", "sql": c, "role": "candidate"}],
                      "meta": {"sqlwitness_index": i, "db_id": db}})
    with open(f"{out}/{ds}_pairs.jsonl", "w") as f:
        for p in pairs:
            f.write(json.dumps(p) + "\n")
    # batches: pairs sharing the same database and the same gold query (normalized text) form one batch
    groups = defaultdict(list)
    for p in pairs:
        groups[(p["meta"]["db_id"], norm_sql(p["queries"][0]["sql"]))].append(p)
    batches = []
    for gi, ((db, _), ps) in enumerate(sorted(groups.items(), key=lambda kv: kv[1][0]["meta"]["sqlwitness_index"])):
        qs = [{"qid": "gold", "sql": ps[0]["queries"][0]["sql"], "role": "reference"}]
        seen = {norm_sql(qs[0]["sql"]): "gold"}
        dup = 0
        for p in ps:
            c = p["queries"][1]["sql"]
            if norm_sql(c) in seen:
                dup += 1
                continue
            qid = f"c{p['meta']['sqlwitness_index']}"
            seen[norm_sql(c)] = qid
            qs.append({"qid": qid, "sql": c, "role": "candidate"})
        batches.append({"batch_id": f"{ds}-b{gi:04d}", "dataset": f"{ds}_batches", "dialect": cfg["dialect"],
                        "schema_path": ps[0]["schema_path"], "queries": qs,
                        "meta": {"db_id": db, "sqlwitness_indices": [p["meta"]["sqlwitness_index"] for p in ps],
                                 "duplicate_candidates_dropped": dup}})
    with open(f"{out}/{ds}_batches.jsonl", "w") as f:
        for b in batches:
            f.write(json.dumps(b) + "\n")
    rep.update(valid_pairs=len(pairs), schemas=len([v for v in cache.values() if v]),
               batches=dist([len(b["queries"]) for b in batches]),
               batches_ge3=sum(len(b["queries"]) >= 3 for b in batches),
               schema_stats=_schema_totals(out, cache))
    REPORT[ds] = rep


def _schema_totals(out, cache):
    tot = Counter()
    for rel in cache.values():
        if rel:
            for k, v in Schema.load(f"{out}/{rel}").stats().items():
                tot[k] += v
    return dict(tot)


def build_leetcode(raw, out):
    art = f"{raw}/sqlwitness/x/SQLWitness-artifact/benchmarks/leetcode"
    valid = {(r["ProblemID"], int(r["QueryID"])): r["Both_Valid"].strip() == "True"
             for r in csv.DictReader(open(f"{raw}/sqlwitness/x/SQLWitness-artifact/results/paper/sanity_check/leetcode_invalid.csv"))}
    rep = {"constraint_parse_fail": []}
    pairs, batches = [], []
    for pid in sorted(f[:-5] for f in os.listdir(f"{art}/schemas") if f.endswith(".json")):
        files = [f"{art}/constraints/{pid}.yml", f"{art}/groundtruths/{pid}.csv", f"{art}/candidates/{pid}.csv"]
        if not all(map(os.path.exists, files)):
            continue
        tables = json.load(open(f"{art}/schemas/{pid}.json")).get("Tables", [])
        text = open(files[0]).read()
        try:
            s = from_sqlwitness(pid, tables, text, source=f"SQLWitness artifact leetcode/{pid}")
        except Exception as e:
            rep["constraint_parse_fail"].append(f"{pid}: {type(e).__name__}: {str(e)[:100]}")
            s = from_sqlwitness(pid, tables, "", source=f"SQLWitness artifact leetcode/{pid}")
            s.native["sqlwitness"]["constraints"] = text
            s.unsupported.append("UNPARSED: " + text[:500])
        s.native["sqlwitness"]["constraints_file"] = f"leetcode/constraints/{pid}.yml"
        sp = write_schema(out, "leetcode", pid, s)
        rows = list(csv.DictReader(open(files[1])))
        rows = rows[1:] if pid == "1789" else rows
        if not rows:
            continue
        gt = rows[0]["mysql_groundtruth"]
        qs = [{"qid": "gold", "sql": gt, "role": "reference"}]
        for r in csv.DictReader(open(files[2])):
            k = (pid, int(r["qid"]))
            if not valid.get(k):
                continue
            lab = {"Y": True, "N": False}.get(r.get("mysql_correct?", "").strip())
            pairs.append({"batch_id": f"leetcode-{pid}-{r['qid']}", "dataset": "leetcode_pairs", "dialect": "mysql",
                          "schema_path": sp, "queries": [qs[0], {"qid": f"c{r['qid']}", "sql": r["mysql_query"],
                                                                 "role": "candidate"}],
                          "meta": {"problem": pid, "query_id": int(r["qid"]), "leetcode_correct": lab}})
            qs.append({"qid": f"c{r['qid']}", "sql": r["mysql_query"], "role": "candidate", "leetcode_correct": lab})
        batches.append({"batch_id": f"leetcode-{pid}", "dataset": "leetcode_batches", "dialect": "mysql",
                        "schema_path": sp, "queries": qs, "meta": {"problem": pid}})
    for name, rows in (("leetcode_pairs", pairs), ("leetcode_batches", batches)):
        with open(f"{out}/{name}.jsonl", "w") as f:
            for b in rows:
                f.write(json.dumps(b) + "\n")
    rep.update(valid_pairs=len(pairs), batches=dist([len(b["queries"]) for b in batches]))
    REPORT["leetcode"] = rep


# ------------------------------------------------------------------ Polygon cubes D-50 / D-100
def build_cubes(raw, out):
    base = f"{raw}/polygon/benchmarks/cubes"
    index = {}
    for line in open(f"{base}/all.jsonlines"):
        d = json.loads(line)
        index[str(d["id"])] = d["schema"]
    for n in (50, 100):
        rep = {"missing_schema": 0}
        cache = {}
        rows = []
        for line in open(f"{base}/benchmarks_{n}_extended.jsonl"):
            b = json.loads(line)
            key = b["benchmark"].replace(".txt", "").replace("-", "/")
            if key not in index or not os.path.exists(f"{base}/schema/{index[key]}"):
                rep["missing_schema"] += 1   # Polygon's own runner skips these (`continue`)
                continue
            sfile = index[key]
            if sfile not in cache:
                tables = json.load(open(f"{base}/schema/{sfile}"))["Tables"]
                name = re.sub(r"[^\w]+", "_", sfile.replace(".json", ""))
                s = from_sqlwitness(name, tables, "", source=f"Polygon artifact benchmarks/cubes/schema/{sfile}")
                cache[sfile] = write_schema(out, "cubes", name, s)
            uniq = len({norm_sql(q) for q in b["queries"]})
            rows.append({"batch_id": f"cubes{n}-{b['id']}", "dataset": f"cubes_d{n}", "dialect": "mysql",
                         "schema_path": cache[sfile],
                         "queries": [{"qid": f"q{i}", "sql": q, "role": "candidate"} for i, q in enumerate(b["queries"])],
                         "meta": {"polygon_id": b["id"], "benchmark": b["benchmark"], "distinct_sql": uniq}})
        with open(f"{out}/cubes_d{n}.jsonl", "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        rep.update(batches=len(rows), queries=sum(len(r["queries"]) for r in rows), schemas=len(cache),
                   distinct_sql_per_batch=dist([r["meta"]["distinct_sql"] for r in rows]))
        REPORT[f"cubes_d{n}"] = rep


# ------------------------------------------------------------------ TPC-H / TPC-DS rewrite batches
def build_rewrites(out, ds):
    if ds == "tpch":
        ddl = open(f"{REPO}/data/tpch_schema.sql").read()
        queries = json.load(open(f"{REPO}/data/tpch_queries.json"))
        rws = [json.loads(l) for l in open(f"{REPO}/data/rewrites.jsonl")]
        labels = {(r["qid"], r["rid"]): r["label"] for r in map(json.loads, open(f"{REPO}/runs/labels.jsonl"))}
        s = from_ddl("tpch", ddl, source="repo data/tpch_schema.sql (TPC-H spec PK/FK incl. composite lineitem->partsupp)")
        seed = "tpch/sf0.01"
    else:
        s = Schema.load(f"{EVAL}/schemas/tpcds.json")
        queries = json.load(open(f"{EVAL}/data/tpcds/queries.json"))
        p = f"{EVAL}/data/tpcds/rewrites.jsonl"
        rws = [json.loads(l) for l in open(p)] if os.path.exists(p) else []
        labels = {}
        seed = "tpcds/sf0.01"
    sp = write_schema(out, ds, ds, s)
    by = defaultdict(list)
    for r in rws:
        by[str(r["qid"])].append(r)
    rows = []
    for q in sorted(queries, key=int):
        qs = [{"qid": "orig", "sql": queries[q], "role": "reference"}]
        for r in sorted(by.get(q, []), key=lambda r: r["rid"]):
            e = {"qid": f"r{r['rid']}", "sql": r["sql"], "role": "candidate", "strategy": r.get("strategy", "")}
            if labels:
                e["bounded_label"] = labels.get((r["qid"], r["rid"]))
            qs.append(e)
        if len(qs) == 1:
            continue
        rows.append({"batch_id": f"{ds}-q{int(q):02d}", "dataset": ds, "dialect": "postgresql", "schema_path": sp,
                     "queries": qs, "meta": {"query": int(q), "seed_db": seed}})
    with open(f"{out}/{ds}.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    REPORT[ds] = {"batches": len(rows), "candidates": sum(len(r["queries"]) - 1 for r in rows),
                  "schema_stats": s.stats(),
                  "labels": dict(Counter(e.get("bounded_label") for r in rows for e in r["queries"][1:])) if labels else None}


XDATA = {"xdatabm": "XData-BM", "tpch_mut": "TPCH"}


def _xd_lines(path, need):
    """XDataN's own reader (test/RegressionTests.java getQueries/getMutants): `id|type|sql`, `--` comments and
    lines with a non-integer id or an empty sql skipped."""
    out = []
    for l in open(path):
        if l.strip().startswith("--"):
            continue
        a = l.rstrip("\n").split("|", 2)
        if len(a) < need or not a[-1].strip():
            continue
        try:
            out.append((int(a[0].strip()), a[1].strip(), a[2].strip()))
        except ValueError:
            continue
    return out


def build_xdata(raw, out, ds):
    """XDataN's home benchmarks (Somwase, Das, Sudarshan, SIGMOD'26): one batch per query, the query as reference
    and its mutants as candidates. The authors label every mutant non-equivalent (`author_label`); the scorer keeps
    that set apart from the known-inequivalent union. Schema = the benchmark's own DDL.sql (CHECKs included)."""
    src = f"{raw}/xdatan/src/benchmarks/{XDATA[ds]}"
    s = from_ddl(ds, open(f"{src}/DDL.sql").read(),
                 source=f"xdatapublic@a860311 benchmarks/{XDATA[ds]}/DDL.sql")
    s.native["xdatan_ddl"] = open(f"{src}/DDL.sql").read()   # XDataN gets its own benchmark DDL verbatim
    sp = write_schema(out, ds, ds, s)
    qs = {i: (ty, q) for i, ty, q in _xd_lines(f"{src}/queries.txt", 2)}
    muts = defaultdict(list)
    for i, ty, m in _xd_lines(f"{src}/mutants.txt", 3):
        muts[i].append((ty, m))
    rows = []
    for i in sorted(qs):
        if not muts.get(i):
            continue
        b = [{"qid": "orig", "sql": qs[i][1], "role": "reference", "query_type": qs[i][0]}]
        b += [{"qid": f"m{k}", "sql": m, "role": "candidate", "mutant_type": ty, "author_label": "neq"}
              for k, (ty, m) in enumerate(muts[i])]
        rows.append({"batch_id": f"{ds}-q{i:03d}", "dataset": ds, "dialect": "postgresql", "schema_path": sp,
                     "queries": b, "meta": {"query": i, "source": f"xdatapublic@a860311 {XDATA[ds]}"}})
    with open(f"{out}/{ds}.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    REPORT[ds] = {"batches": len(rows), "candidates": sum(len(r["queries"]) - 1 for r in rows),
                  "duplicate_candidates": sum(len(r["queries"]) - 1 - len({norm_sql(x["sql"]) for x in r["queries"][1:]})
                                              for r in rows),
                  "schema_stats": s.stats()}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="spider,bird,minidev_pg,leetcode,cubes,tpch,tpcds")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    for ds in a.only.split(","):
        if ds in SW:
            build_sqlwitness(a.raw, a.out, ds)
        elif ds == "leetcode":
            build_leetcode(a.raw, a.out)
        elif ds == "cubes":
            build_cubes(a.raw, a.out)
        elif ds in XDATA:
            build_xdata(a.raw, a.out, ds)
        else:
            build_rewrites(a.out, ds)
        print(ds, json.dumps(REPORT.get(ds) or REPORT.get("cubes_d50"), default=str)[:1500], flush=True)
    rp = f"{a.out}/build_report.json"
    old = json.load(open(rp)) if os.path.exists(rp) else {}
    old.update(REPORT)
    json.dump(old, open(rp, "w"), indent=1, default=str)
