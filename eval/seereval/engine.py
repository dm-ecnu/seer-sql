"""Execute queries on a dialect's real engine over a concrete database instance, with every declarable
constraint in the DDL, and compare results.

A database instance is {table: [ {col: value}, ... ]} (column names matched case-insensitively).
Engines: sqlite (stdlib, in memory), postgresql (psycopg2; one scratch schema per instance), mysql (PyMySQL; one
scratch database per instance), duckdb (optional, for reference/labels).

Connection URLs (no passwords in code):
  SEER_PG_URL     default postgresql://probe@127.0.0.1:55917/postgres
  SEER_MYSQL_URL  default mysql://root:@127.0.0.1:23306/testdb   (password from SEER_MYSQL_PASSWORD)
"""
from __future__ import annotations

import datetime
import decimal
import math
import os
import re
import sqlite3
import time
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .schema import Schema, to_ddl, q_ident


def pyformat_escape(sql_text: str) -> str:
    """Escape '%' for drivers with pyformat parameters (psycopg2, pymysql): a quoted identifier such as
    "Percent (%) Eligible" would otherwise be read as a placeholder when the INSERT carries row parameters."""
    return sql_text.replace("%", "%%")


class LoadError(Exception):
    """The engine rejected the instance (constraint violation or type error)."""


class ExecError(Exception):
    pass


def normalize_sql(sql: str, dialect: str) -> str:
    """Lowercase SQL keywords/identifiers while preserving string literals; copied from SQLWitness
    (`sqlwitness._lowercase_sql_preserve_literals`, Apache-2.0) so that every tool's queries run exactly as
    SQLWitness runs them. PostgreSQL double-quoted identifiers are lowercased too."""
    parts = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`)", sql)
    out = []
    for i, part in enumerate(parts):
        if part.startswith("'"):
            out.append(part)
        elif part.startswith("`"):
            out.append(part.lower() if dialect == "mysql" else part)
        elif part.startswith('"'):
            out.append('"' + part[1:-1].lower() + '"' if dialect == "postgresql" else part)
        else:
            out.append(part.lower())
    return "".join(out)


# ---------------------------------------------------------------- value normalisation for comparison
def _norm(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int,)):
        return v
    if isinstance(v, decimal.Decimal):
        v = float(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if v == int(v) and abs(v) < 1e15:
            return int(v)
        return round(v, 6)
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    if isinstance(v, datetime.timedelta):
        return v.total_seconds()
    return str(v)


def normalize_rows(rows) -> List[tuple]:
    return [tuple(_norm(x) for x in r) for r in rows]


def has_top_order_by(sql: str, dialect: str) -> bool:
    try:
        import sqlglot
        tree = sqlglot.parse_one(sql, read={"postgresql": "postgres"}.get(dialect, dialect))
        return tree.args.get("order") is not None
    except Exception:
        return bool(re.search(r"order\s+by[^)]*$", sql, re.I | re.S))


def result_key(rows: List[tuple], ordered: bool):
    """Canonical, hashable form of a result: list semantics if ordered, else bag semantics."""
    if ordered:
        return ("L", tuple(rows))
    return ("B", tuple(sorted(Counter(rows).items(), key=lambda kv: repr(kv))))


# ---------------------------------------------------------------- engines
class Engine:
    dialect = ""

    def __init__(self, schema: Schema, statement_timeout_s: float = 30, bare: bool = False):
        self.schema = schema
        self.timeout = statement_timeout_s
        self.bare = bare

    # instance -> engine; raises LoadError
    def load(self, instance: Dict[str, List[Dict[str, Any]]]):
        raise NotImplementedError

    def run(self, sql: str) -> List[tuple]:
        raise NotImplementedError

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _rows_for(self, instance, table):
        for k, v in instance.items():
            if k.lower() == table.name.lower():
                return v or []
        return []

    def _row_values(self, table, row):
        low = {str(k).lower(): v for k, v in row.items()}
        return [_coerce(low.get(c["name"].lower()), c["type"]) for c in table.columns]


def _coerce(v, ty):
    """Tools return strings for everything (SQLWitness CSV-ish values, ParSEval str()); coerce to the column type
    so that the engine sees the intended value. Unparseable values are passed through and the engine decides."""
    if v is None:
        return None
    if isinstance(v, str) and v.upper() == "NULL":
        return None
    try:
        if ty == "int":
            if isinstance(v, bool):
                return int(v)
            if isinstance(v, (int,)):
                return v
            f = float(v)
            return int(f) if f == int(f) else f
        if ty in ("real", "numeric"):
            if isinstance(v, (int, float, decimal.Decimal)):
                return v
            return float(v) if ty == "real" else decimal.Decimal(str(v))
        if ty == "boolean":
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "t", "yes")
            return bool(v)
    except (ValueError, TypeError, decimal.InvalidOperation):
        return v
    if ty == "varchar" and not isinstance(v, str):
        return str(v)
    return v


class SQLiteEngine(Engine):
    dialect = "sqlite"

    def __init__(self, schema, statement_timeout_s=30, bare=False):
        super().__init__(schema, statement_timeout_s, bare)
        self.con = sqlite3.connect(":memory:", isolation_level=None)
        self.con.execute("PRAGMA foreign_keys=ON")
        for st in to_ddl(schema, "sqlite", deferrable=True, bare=bare):
            self.con.execute(st)

    def load(self, instance):
        cur = self.con.cursor()
        try:
            cur.execute("BEGIN")
            for tn in self.schema.topo_order():
                t = self.schema.table(tn)
                cols = ", ".join(q_ident(c["name"], "sqlite") for c in t.columns)
                ph = ", ".join("?" * len(t.columns))
                for r in self._rows_for(instance, t):
                    cur.execute(f'INSERT INTO {q_ident(t.name, "sqlite")} ({cols}) VALUES ({ph})', self._row_values(t, r))
            cur.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                cur.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise LoadError(f"{type(e).__name__}: {e}")

    def run(self, sql):
        deadline = time.time() + self.timeout
        self.con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
        try:
            return normalize_rows(self.con.execute(normalize_sql(sql, "sqlite")).fetchall())
        except sqlite3.Error as e:
            raise ExecError(f"{type(e).__name__}: {e}")
        finally:
            self.con.set_progress_handler(None, 0)

    def close(self):
        self.con.close()


class PostgresEngine(Engine):
    dialect = "postgresql"

    def __init__(self, schema, statement_timeout_s=30, bare=False):
        super().__init__(schema, statement_timeout_s, bare)
        import psycopg2
        self.pg = psycopg2
        self.con = psycopg2.connect(os.environ.get("SEER_PG_URL", "postgresql://probe@127.0.0.1:55917/postgres"))
        self.con.autocommit = True
        self.ns = "seer_v_" + uuid.uuid4().hex[:12]
        cur = self.con.cursor()
        cur.execute(f"CREATE SCHEMA {self.ns}")
        cur.execute(f"SET search_path TO {self.ns}")
        cur.execute(f"SET statement_timeout = {int(self.timeout * 1000)}")
        for st in to_ddl(schema, "postgresql", deferrable=True, bare=bare):
            cur.execute(st)

    def load(self, instance):
        cur = self.con.cursor()
        try:
            cur.execute("BEGIN")
            for tn in self.schema.topo_order():
                t = self.schema.table(tn)
                cols = ", ".join(q_ident(c["name"], "postgresql") for c in t.columns)
                ph = ", ".join(["%s"] * len(t.columns))
                sql = pyformat_escape(f'INSERT INTO {q_ident(t.name, "postgresql")} ({cols})') + f" VALUES ({ph})"
                for r in self._rows_for(instance, t):
                    cur.execute(sql, self._row_values(t, r))
            cur.execute("COMMIT")
        except self.pg.Error as e:
            cur.execute("ROLLBACK")
            raise LoadError(f"{type(e).__name__}: {str(e).strip()[:300]}")

    def run(self, sql):
        cur = self.con.cursor()
        try:
            cur.execute(normalize_sql(sql, "postgresql"))
            return normalize_rows(cur.fetchall())
        except self.pg.Error as e:
            raise ExecError(f"{type(e).__name__}: {str(e).strip()[:300]}")

    def close(self):
        try:
            self.con.cursor().execute(f"DROP SCHEMA IF EXISTS {self.ns} CASCADE")
        finally:
            self.con.close()


class MySQLEngine(Engine):
    dialect = "mysql"

    def __init__(self, schema, statement_timeout_s=30, bare=False):
        super().__init__(schema, statement_timeout_s, bare)
        import pymysql
        from urllib.parse import urlparse
        self.my = pymysql
        u = urlparse(os.environ.get("SEER_MYSQL_URL", "mysql://root@127.0.0.1:23306/testdb"))
        self.db = "seer_v_" + uuid.uuid4().hex[:12]
        self.con = pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username,
                                   password=u.password or os.environ.get("SEER_MYSQL_PASSWORD", ""), autocommit=True)
        cur = self.con.cursor()
        cur.execute(f"CREATE DATABASE {self.db}")
        cur.execute(f"USE {self.db}")
        cur.execute(f"SET SESSION max_execution_time = {int(self.timeout * 1000)}")
        cur.execute("SET SESSION sql_mode = 'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
                    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")
        for st in to_ddl(schema, "mysql", deferrable=False, bare=bare):
            cur.execute(st)

    def load(self, instance):
        cur = self.con.cursor()
        try:
            cur.execute("START TRANSACTION")
            for tn in self.schema.topo_order():
                t = self.schema.table(tn)
                cols = ", ".join(q_ident(c["name"], "mysql") for c in t.columns)
                ph = ", ".join(["%s"] * len(t.columns))
                sql = pyformat_escape(f'INSERT INTO {q_ident(t.name, "mysql")} ({cols})') + f" VALUES ({ph})"
                for r in self._rows_for(instance, t):
                    cur.execute(sql, self._row_values(t, r))
            cur.execute("COMMIT")
        except self.my.Error as e:
            cur.execute("ROLLBACK")
            raise LoadError(f"{type(e).__name__}: {str(e)[:300]}")

    def run(self, sql):
        cur = self.con.cursor()
        try:
            cur.execute(normalize_sql(sql, "mysql"))
            return normalize_rows(cur.fetchall())
        except self.my.Error as e:
            raise ExecError(f"{type(e).__name__}: {str(e)[:300]}")

    def close(self):
        try:
            self.con.cursor().execute(f"DROP DATABASE IF EXISTS {self.db}")
        finally:
            self.con.close()


class DuckDBEngine(Engine):
    dialect = "duckdb"

    def __init__(self, schema, statement_timeout_s=30, bare=False):
        super().__init__(schema, statement_timeout_s, bare)
        import duckdb
        self.duck = duckdb
        self.con = duckdb.connect()
        # DuckDB checks FKs eagerly and cannot defer; tables are created without FKs, FKs are checked in Python.
        for st in to_ddl(schema, "duckdb", with_fk=False, bare=bare):
            self.con.execute(st)

    def load(self, instance):
        try:
            for tn in self.schema.topo_order():
                t = self.schema.table(tn)
                rows = [self._row_values(t, r) for r in self._rows_for(instance, t)]
                if rows:
                    self.con.executemany(f'INSERT INTO {q_ident(t.name, "duckdb")} VALUES (' + ",".join("?" * len(t.columns)) + ")", rows)
        except self.duck.Error as e:
            raise LoadError(f"{type(e).__name__}: {str(e)[:300]}")

    def run(self, sql):
        try:
            return normalize_rows(self.con.execute(normalize_sql(sql, "duckdb")).fetchall())
        except self.duck.Error as e:
            raise ExecError(f"{type(e).__name__}: {str(e)[:300]}")

    def close(self):
        self.con.close()


ENGINES = {"sqlite": SQLiteEngine, "postgresql": PostgresEngine, "mysql": MySQLEngine, "duckdb": DuckDBEngine}


def make_engine(dialect: str, schema: Schema, timeout_s: float = 30, bare: bool = False) -> Engine:
    return ENGINES[dialect](schema, timeout_s, bare)


# ---------------------------------------------------------------- python-side constraint check
def python_violations(schema: Schema, instance) -> List[str]:
    """Constraints the DDL cannot carry (or the engine does not enforce): FK MATCH SIMPLE incl. composite,
    inclusion dependencies to non-key columns, enums, domains, PK/UNIQUE/NOT NULL (double check)."""
    out = []

    def rows(tn):
        for k, v in instance.items():
            if k.lower() == tn.lower():
                return v or []
        return []

    def get(r, c):
        for k, v in r.items():
            if str(k).lower() == c.lower():
                return None if (isinstance(v, str) and v.upper() == "NULL") else v
        return None

    def key(r, cols, tys):
        return tuple(str(_coerce(get(r, c), ty)) for c, ty in zip(cols, tys))

    for t in schema.tables:
        tys = {c["name"].lower(): c["type"] for c in t.columns}
        rs = rows(t.name)
        for c in t.columns:
            if c.get("not_null") or c["name"] in t.primary_key:
                if any(get(r, c["name"]) is None for r in rs):
                    out.append(f"NOT NULL {t.name}.{c['name']}")
        for k in ([t.primary_key] if t.primary_key else []) + t.unique:
            seen = set()
            for r in rs:
                if any(get(r, c) is None for c in k):
                    continue
                kv = key(r, k, [tys[c.lower()] for c in k])
                if kv in seen:
                    out.append(f"{'PK' if k == t.primary_key else 'UNIQUE'} {t.name}({','.join(k)})")
                    break
                seen.add(kv)
        for fk in t.foreign_keys + [i for i in schema.inclusions if i["table"].lower() == t.name.lower()]:
            p = schema.table(fk["ref_table"])
            ptys = {c["name"].lower(): c["type"] for c in p.columns}
            ctys = [ptys[c.lower()] for c in fk["ref_columns"]]
            parent = {key(r, fk["ref_columns"], ctys) for r in rows(p.name)}
            for r in rs:
                if any(get(r, c) is None for c in fk["columns"]):
                    continue  # MATCH SIMPLE
                if key(r, fk["columns"], ctys) not in parent:
                    out.append(f"FK {t.name}({','.join(fk['columns'])})->{p.name}")
                    break
    for e in schema.enums:
        vals = {str(v) for v in e["values"]}
        for r in rows(e["table"]):
            v = get(r, e["column"])
            if v is not None and str(v) not in vals and str(_coerce(v, "int")) not in vals:
                out.append(f"ENUM {e['table']}.{e['column']}")
                break
    for d in schema.domains:
        for r in rows(d["table"]):
            v = get(r, d["column"])
            if v is None:
                continue
            try:
                if isinstance(d["lo"], (int, float)):
                    ok = float(d["lo"]) <= float(v) <= float(d["hi"])
                else:
                    ok = str(d["lo"]) <= str(v)[:10] <= str(d["hi"])
            except (TypeError, ValueError):
                ok = False
            if not ok:
                out.append(f"DOMAIN {d['table']}.{d['column']}")
                break
    return out
