"""TPC-DS schema (full PK/FK incl. the spec's 3 composite FKs), the 99 queries, and an optional SF data seed.

  python eval/build/build_tpcds.py schema            # -> eval/data/tpcds/tpcds_schema.sql, eval/schemas/tpcds.json
  python eval/build/build_tpcds.py queries           # -> eval/data/tpcds/queries.json (DuckDB tpcds extension)
  python eval/build/build_tpcds.py check --sf 0.01 --out DIR   # dsdgen in DuckDB, write DIR/sf0.01/*.parquet,
        load into PostgreSQL ($SEER_PG_URL) with all constraints, run the 99 queries on DuckDB and PostgreSQL

Sources: tools/tpcds.sql and tools/tpcds_ri.sql of gregrahn/tpcds-kit @5a3a817 (TPC-DS v2.x kit; PKs and the
single-column FKs), plus data/tpcds/kit/composite_fk.sql (spec). The `dbgen_version` table is dropped.
Queries: DuckDB `tpcds_queries()` (duckdb==1.5.6), i.e. the TPC-DS templates with the qualification substitutions.
"""
import argparse, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL = os.path.dirname(HERE)
sys.path.insert(0, EVAL)
from seereval.schema import from_ddl, to_ddl, Schema  # noqa: E402

KIT = f"{EVAL}/data/tpcds/kit"


def build_schema() -> Schema:
    ddl = "".join(open(f"{KIT}/{f}").read() for f in ("tpcds.sql", "tpcds_ri.sql", "composite_fk.sql"))
    s = from_ddl("tpcds", ddl, source="gregrahn/tpcds-kit@5a3a817 tools/tpcds.sql+tpcds_ri.sql + spec composite FKs")
    s.tables = [t for t in s.tables if t.name != "dbgen_version"]
    return s


def cmd_schema(_):
    s = build_schema()
    os.makedirs(f"{EVAL}/schemas", exist_ok=True)
    s.dump(f"{EVAL}/schemas/tpcds.json")
    with open(f"{EVAL}/data/tpcds/tpcds_schema.sql", "w") as f:
        for st in to_ddl(s, "postgresql", deferrable=False, quote=False):
            f.write(st + ";\n")
    print(s.stats())


def cmd_queries(_):
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL tpcds; LOAD tpcds")
    rows = con.execute("SELECT query_nr, query FROM tpcds_queries() ORDER BY query_nr").fetchall()
    q = {str(n): sql.strip().rstrip(";") for n, sql in rows}
    json.dump(q, open(f"{EVAL}/data/tpcds/queries.json", "w"), indent=1)
    print(len(q), "queries, duckdb", duckdb.__version__)


def cmd_check(a):
    import duckdb, psycopg2
    s = build_schema()
    out = f"{a.out}/sf{a.sf}"
    os.makedirs(out, exist_ok=True)
    con = duckdb.connect()
    con.execute("INSTALL tpcds; LOAD tpcds")
    t = time.time()
    con.execute(f"CALL dsdgen(sf={a.sf})")
    print(f"dsdgen sf={a.sf}: {time.time() - t:.1f}s")
    counts = {}
    for tb in s.tables:
        con.execute(f"COPY {tb.name} TO '{out}/{tb.name}.parquet' (FORMAT parquet)")
        counts[tb.name] = con.execute(f"SELECT count(*) FROM {tb.name}").fetchone()[0]
    # constraint check on the generated data (DuckDB-side, set based)
    viol = {}
    for tb in s.tables:
        if tb.primary_key:
            k = ", ".join(tb.primary_key)
            n = con.execute(f"SELECT count(*) FROM (SELECT {k} FROM {tb.name} GROUP BY {k} HAVING count(*) > 1)").fetchone()[0]
            if n:
                viol[f"PK {tb.name}"] = n
        for fk in tb.foreign_keys:
            cond = " AND ".join(f"c.{x} = p.{y}" for x, y in zip(fk["columns"], fk["ref_columns"]))
            nn = " AND ".join(f"c.{x} IS NOT NULL" for x in fk["columns"])
            n = con.execute(f"SELECT count(*) FROM {tb.name} c WHERE {nn} AND NOT EXISTS "
                            f"(SELECT 1 FROM {fk['ref_table']} p WHERE {cond})").fetchone()[0]
            if n:
                viol[f"FK {tb.name}({','.join(fk['columns'])})->{fk['ref_table']}"] = n
    queries = json.load(open(f"{EVAL}/data/tpcds/queries.json"))
    duck_ok = {}
    for k, sql in queries.items():
        try:
            con.execute(sql).fetchall(); duck_ok[k] = True
        except Exception as e:
            duck_ok[k] = f"{type(e).__name__}: {str(e)[:120]}"
    # PostgreSQL: load with all constraints (FKs added after the load), run the queries
    pg = psycopg2.connect(os.environ.get("SEER_PG_URL", "postgresql://probe@127.0.0.1:55917/postgres"))
    pg.autocommit = True
    cur = pg.cursor()
    db = f"tpcds_sf{str(a.sf).replace('.', '_')}"
    cur.execute(f"DROP SCHEMA IF EXISTS {db} CASCADE; CREATE SCHEMA {db}; SET search_path TO {db}")
    cur.execute("SET statement_timeout = 120000")
    for st in to_ddl(s, "postgresql", with_fk=False, quote=False):
        cur.execute(st)
    for tb in s.tables:
        con.execute(f"COPY {tb.name} TO '{out}/{tb.name}.csv' (HEADER false, DELIMITER '|', NULL '')")
        with open(f"{out}/{tb.name}.csv") as f:
            cur.copy_expert(f"COPY {tb.name} FROM STDIN WITH (FORMAT csv, DELIMITER '|', NULL '')", f)
        os.remove(f"{out}/{tb.name}.csv")
    pg_fk_err = {}
    for tb in s.tables:
        for i, fk in enumerate(tb.foreign_keys):
            try:
                cur.execute(f"ALTER TABLE {tb.name} ADD CONSTRAINT fk_{tb.name}_{i} FOREIGN KEY ({', '.join(fk['columns'])}) "
                            f"REFERENCES {fk['ref_table']} ({', '.join(fk['ref_columns'])})")
            except psycopg2.Error as e:
                pg_fk_err[f"{tb.name}({','.join(fk['columns'])})"] = str(e).splitlines()[0][:120]
    pg_ok = {}
    for k, sql in queries.items():
        try:
            cur.execute(sql); cur.fetchall(); pg_ok[k] = True
        except psycopg2.Error as e:
            pg_ok[k] = f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"
    rep = {"sf": a.sf, "rows": counts, "violations_duckdb": viol, "pg_fk_errors": pg_fk_err,
           "duck_fail": {k: v for k, v in duck_ok.items() if v is not True},
           "pg_fail": {k: v for k, v in pg_ok.items() if v is not True}, "pg_schema": db}
    json.dump(rep, open(f"{out}/check.json", "w"), indent=1)
    print(json.dumps({k: (v if k not in ("rows",) else sum(v.values())) for k, v in rep.items()}, indent=1)[:4000])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("schema"); sp.add_parser("queries")
    c = sp.add_parser("check"); c.add_argument("--sf", default="0.01"); c.add_argument("--out", required=True)
    a = ap.parse_args()
    {"schema": cmd_schema, "queries": cmd_queries, "check": cmd_check}[a.cmd](a)
