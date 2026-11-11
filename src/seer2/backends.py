"""Execution backends. Each instance is loaded into a fresh database whose DDL declares every constraint, so the
engine double-checks validity (an insert error raises EngineRejected, which the refiner treats as a bug).

DuckDB (primary): in-memory connection per instance. FKs that close a cycle are left out of DuckDB's DDL (DuckDB
cannot defer them) and are checked by Schema.violations only.
SQLite: in-memory, PRAGMA foreign_keys=ON.
PostgreSQL (psycopg 3): one schema per instance inside a transaction that is rolled back; each query runs under a
savepoint with statement_timeout. DSN from SEER2_PG_DSN (default: the server PG16, host=127.0.0.1 port=55917 user=probe).
MySQL (pymysql): one database per instance, dropped on close; the session uses the sql_mode of the shared validator
(ONLY_FULL_GROUP_BY with functional-dependence detection, strict). Queries are lower-cased outside literals, as
SQLWitness runs them. URL from SEER_MYSQL_URL (mysql://user:password@host:port/db).
"""
from __future__ import annotations

import datetime as dt
import decimal
import os
import threading
import time

import sqlglot

from .schema import Schema


class EngineRejected(Exception):
    pass


class QueryError:
    def __init__(self, msg):
        self.msg = msg

    def __repr__(self):
        return f"ERR({self.msg[:120]})"


def transpile(sql, read, write):
    if read == write:
        return sql
    return sqlglot.transpile(sql, read=read, write=write)[0]


def _q(name):
    """Identifier as the DDL from Schema.ddl_for spells it."""
    from .schema import qid
    return qid(name)


class Backend:
    dialect = "duckdb"

    def __init__(self, schema: Schema, timeout=10.0):
        self.schema, self.timeout = schema, timeout

    def open(self, db):
        raise NotImplementedError


class _DuckInst:
    def __init__(self, con, timeout):
        self.con, self.timeout = con, timeout

    def run(self, sql):
        timer = threading.Timer(self.timeout, self.con.interrupt)
        timer.start()
        try:
            cur = self.con.execute(sql)
            rows = cur.fetchall()
            return rows, len(cur.description or [])
        except Exception as e:
            return QueryError(f"{type(e).__name__}: {e}"), 0
        finally:
            timer.cancel()

    def close(self):
        self.con.close()


class DuckDBBackend(Backend):
    dialect = "duckdb"

    def __init__(self, schema, timeout=10.0):
        super().__init__(schema, timeout)
        self.ddl = schema.ddl_for("duckdb", deferred_fk=False)

    def open(self, db):
        import duckdb
        con = duckdb.connect(config={"threads": 1})
        for st in self.ddl:
            con.execute(st)
        try:
            for t in self.schema.order:
                rows = db.get(t, [])
                if not rows:
                    continue
                cols = self.schema.tables[t].colnames
                con.executemany(f"INSERT INTO {_q(t)} ({', '.join(map(_q, cols))}) VALUES ({', '.join('?' * len(cols))})",
                                [[r[c] for c in cols] for r in rows])
        except Exception as e:
            con.close()
            raise EngineRejected(f"duckdb rejected instance: {e}")
        return _DuckInst(con, self.timeout)


def _sqlite_val(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


class _SqliteInst:
    def __init__(self, con, timeout):
        self.con, self.timeout = con, timeout

    def run(self, sql):
        t0 = time.monotonic()
        self.con.set_progress_handler(lambda: 1 if time.monotonic() - t0 > self.timeout else 0, 10000)
        try:
            cur = self.con.execute(sql)
            rows = cur.fetchall()
            return rows, len(cur.description or [])
        except Exception as e:
            return QueryError(f"{type(e).__name__}: {e}"), 0
        finally:
            self.con.set_progress_handler(None, 0)

    def close(self):
        self.con.close()


class SQLiteBackend(Backend):
    dialect = "sqlite"

    def __init__(self, schema, timeout=10.0):
        super().__init__(schema, timeout)
        self.ddl = schema.ddl_for("sqlite")

    def open(self, db):
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.execute("PRAGMA foreign_keys=ON")
        for st in self.ddl:
            con.execute(st)
        try:
            # FKs are checked on the final instance (as Σ is), not row by row: a self-referencing or cyclic FK may
            # point to a row inserted later in the same load
            con.execute("BEGIN")
            con.execute("PRAGMA defer_foreign_keys=ON")
            for t in self.schema.order:
                rows = db.get(t, [])
                if not rows:
                    continue
                cols = self.schema.tables[t].colnames
                con.executemany(f"INSERT INTO {_q(t)} ({', '.join(map(_q, cols))}) VALUES ({', '.join('?' * len(cols))})",
                                [[_sqlite_val(r[c]) for c in cols] for r in rows])
            con.commit()
        except Exception as e:
            con.close()
            raise EngineRejected(f"sqlite rejected instance: {e}")
        return _SqliteInst(con, self.timeout)


class _PgInst:
    def __init__(self, con, timeout):
        self.con, self.timeout = con, timeout

    def run(self, sql):
        cur = self.con.cursor()
        try:
            cur.execute("SAVEPOINT q")
            cur.execute(f"SET LOCAL statement_timeout = {int(self.timeout * 1000)}")
            cur.execute(sql)
            rows = cur.fetchall()
            n = len(cur.description or [])
            cur.execute("RELEASE SAVEPOINT q")
            return rows, n
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT q")
            return QueryError(f"{type(e).__name__}: {e}"), 0

    def close(self):
        self.con.rollback()


class PostgresBackend(Backend):
    dialect = "postgres"

    def __init__(self, schema, timeout=10.0, dsn=None):
        super().__init__(schema, timeout)
        import psycopg
        self.dsn = dsn or os.environ.get("SEER2_PG_DSN", "host=127.0.0.1 port=55917 dbname=postgres user=probe")
        self.con = psycopg.connect(self.dsn, autocommit=False)
        self.ddl = schema.ddl_for("postgres")
        self.n = 0

    def open(self, db):
        self.n += 1
        cur = self.con.cursor()
        sch = f"seer2_{os.getpid()}_{self.n}"
        cur.execute(f"CREATE SCHEMA {sch}")
        cur.execute(f"SET LOCAL search_path TO {sch}")
        for st in self.ddl:
            cur.execute(st)
        try:
            for t in self.schema.order:
                rows = db.get(t, [])
                if not rows:
                    continue
                cols = self.schema.tables[t].colnames
                # '%' in an identifier (BIRD: "Percent (%) Eligible ...") is literal text, not a placeholder
                pq = lambda x: _q(x).replace("%", "%%")
                cur.executemany(f"INSERT INTO {pq(t)} ({', '.join(map(pq, cols))}) VALUES ({', '.join(['%s'] * len(cols))})",
                                [[r[c] for c in cols] for r in rows])
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        except Exception as e:
            self.con.rollback()
            raise EngineRejected(f"postgres rejected instance: {e}")
        return _PgInst(self.con, self.timeout)


MYSQL_MODE = ("ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,"
              "NO_ENGINE_SUBSTITUTION")


def mysql_lower(sql):
    """Lower-case keywords and identifiers outside string literals (SQLWitness's query convention for MySQL)."""
    import re
    parts = re.split(r"('(?:''|\\.|[^'\\])*'|\"(?:\"\"|[^\"])*\")", sql)
    return "".join(p if p[:1] in ("'", '"') else p.lower() for p in parts)


class _MyInst:
    def __init__(self, con, name, timeout):
        self.con, self.name, self.timeout = con, name, timeout

    def run(self, sql):
        cur = self.con.cursor()
        try:
            cur.execute(mysql_lower(sql))
            rows = cur.fetchall()
            return list(rows), len(cur.description or [])
        except Exception as e:
            # A lost MySQL session is an infrastructure failure, not a SQL
            # result. Treating it as an error value can corrupt a witness
            # fingerprint and make a later independent replay disagree.
            code = e.args[0] if e.args and isinstance(e.args[0], int) else None
            if code in (1040, 2002, 2003, 2006, 2013, 2055):
                raise EngineRejected(f"mysql connection failure ({code})") from e
            import re
            message = re.sub(r"\bseer2_\d+_\d+\.", "<scratch-db>.", str(e))
            return QueryError(f"{type(e).__name__}: {message}"), 0

    def close(self):
        try:
            self.con.cursor().execute(f"DROP DATABASE IF EXISTS {self.name}")
        except Exception:
            pass


class MySQLBackend(Backend):
    dialect = "mysql"
    deferred_fks = False        # FK checks are immediate: no instance with a reference cycle can be loaded

    def __init__(self, schema, timeout=10.0, url=None):
        super().__init__(schema, timeout)
        import pymysql
        from urllib.parse import urlparse
        u = urlparse(url or os.environ.get("SEER_MYSQL_URL", "mysql://root@127.0.0.1:23306/testdb"))
        self.con = pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username, password=u.password or "",
                                   autocommit=True)
        cur = self.con.cursor()
        cur.execute(f"SET SESSION sql_mode = '{MYSQL_MODE}'")
        cur.execute(f"SET SESSION max_execution_time = {int(timeout * 1000)}")
        self.ddl = schema.ddl_for("mysql")
        self.n = 0

    def open(self, db):
        self.n += 1
        name = f"seer2_{os.getpid()}_{self.n}"
        cur = self.con.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS {name}")
        cur.execute(f"CREATE DATABASE {name}")
        cur.execute(f"USE {name}")
        try:
            for st in self.ddl:
                cur.execute(st)
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")      # Σ holds on the final instance; checked below
            for t in self.schema.order:
                rows = db.get(t, [])
                if not rows:
                    continue
                cols = self.schema.tables[t].colnames
                bq = lambda x: ("`" + x.replace("`", "``") + "`").replace("%", "%%")   # literal '%' (parametrised)
                cur.executemany(f"INSERT INTO {bq(t)} ({', '.join(map(bq, cols))}) VALUES ({', '.join(['%s'] * len(cols))})",
                                [[r[c] for c in cols] for r in rows])
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            for t in self.schema.order:                    # FKs on the loaded instance (MATCH SIMPLE)
                for fk in self.schema.tables[t].fks:
                    if not fk.ddl:
                        continue
                    bq = lambda x: "`" + x.replace("`", "``") + "`"
                    on = " AND ".join(f"p.{bq(b)} = c.{bq(a)}" for a, b in zip(fk.cols, fk.ref_cols))
                    nn = " AND ".join(f"c.{bq(a)} IS NOT NULL" for a in fk.cols)
                    cur.execute(f"SELECT 1 FROM {bq(t)} c WHERE {nn} AND NOT EXISTS (SELECT 1 FROM {bq(fk.ref)} p "
                                f"WHERE {on}) LIMIT 1")
                    if cur.fetchall():
                        raise ValueError(f"FOREIGN KEY {t}{tuple(fk.cols)} -> {fk.ref} violated")
        except Exception as e:
            try:
                cur.execute(f"DROP DATABASE IF EXISTS {name}")
            except Exception:
                pass
            raise EngineRejected(f"mysql rejected instance: {e}")
        return _MyInst(self.con, name, self.timeout)


def make_backend(name, schema, timeout=10.0):
    return {"duckdb": DuckDBBackend, "sqlite": SQLiteBackend, "postgres": PostgresBackend,
            "mysql": MySQLBackend}[name](schema, timeout)
