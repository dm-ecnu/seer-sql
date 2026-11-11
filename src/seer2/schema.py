"""Generic DDL parser (sqlglot) and constraint checker.

Supported: column types, PRIMARY KEY (column or table level, composite), UNIQUE (column or table level), NOT NULL,
FOREIGN KEY (column-level REFERENCES or table-level, composite; MATCH SIMPLE: a row whose FK columns contain any NULL
satisfies the FK), simple CHECK (column op constant, column op column, BETWEEN, IN-list, IS [NOT] NULL, AND of those).
Tables are ordered topologically; a cyclic FK is broken at a nullable column (that FK is "deferred": the generator only
points it to rows that already exist, or sets it to NULL).
"""
from __future__ import annotations

import datetime as dt
import decimal
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

INT, DEC, FLOAT, STR, DATE, BOOL, TS = "int", "dec", "float", "str", "date", "bool", "ts"


def qid(name):
    """Identifier for generated DDL/DML. Lower-case names are always quoted (case-safe in every engine, and a column
    called `cross` or `order` must not parse as a keyword); other names are quoted only when they need it, so that
    unquoted references in the queries still fold to them."""
    return exp.to_identifier(name, quoted=True if name == name.lower() else None).sql(dialect="duckdb")

_T = exp.DataType.Type
_INT_T = {_T.INT, _T.BIGINT, _T.SMALLINT, _T.TINYINT, _T.UINT, _T.UBIGINT, _T.USMALLINT, _T.UTINYINT, _T.INT128,
          _T.MEDIUMINT}
_DEC_T = {_T.DECIMAL, _T.MONEY, _T.SMALLMONEY}
_FLOAT_T = {_T.FLOAT, _T.DOUBLE}
_DATE_T = {_T.DATE}
_TS_T = {_T.TIMESTAMP, _T.DATETIME, _T.TIMESTAMPTZ, _T.TIMESTAMPLTZ}
_BOOL_T = {_T.BOOLEAN}


def map_type(dtype: exp.DataType):
    t = dtype.this
    if t in _INT_T:
        return INT, None
    if t in _DEC_T:
        ps = [int(p.this.this) for p in dtype.expressions if isinstance(p.this, exp.Literal)] if dtype.expressions else []
        scale = ps[1] if len(ps) > 1 else 2
        prec = ps[0] if ps else 18
        return DEC, (prec, scale)
    if t in _FLOAT_T:
        return FLOAT, None
    if t in _DATE_T:
        return DATE, None
    if t in _TS_T:
        return TS, None
    if t in _BOOL_T:
        return BOOL, None
    return STR, None


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    raw: str
    param: tuple | None = None


@dataclass
class FK:
    cols: list
    ref: str
    ref_cols: list
    deferred: bool = False      # set when the FK closes a cycle
    ddl: bool = True            # False: an inclusion dependency Σ contains but SQL cannot declare (non-key parent)


@dataclass
class Check:
    expr: exp.Expression        # with unqualified column names
    cols: list                  # referenced column names


@dataclass
class Table:
    name: str
    columns: list
    pk: list = field(default_factory=list)
    uniques: list = field(default_factory=list)
    fks: list = field(default_factory=list)
    checks: list = field(default_factory=list)

    def col(self, name):
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"{self.name}.{name}")

    @property
    def colnames(self):
        return [c.name for c in self.columns]


def _ident(x):
    return (x.name if hasattr(x, "name") else str(x)).lower()


class Schema:
    def __init__(self, ddl: str, dialect: str = "duckdb", inclusions=()):
        """inclusions: [(table, cols, ref_table, ref_cols)] constraints of Σ that the DDL cannot carry (a FOREIGN KEY
        to non-key columns: every non-NULL child tuple must occur in the parent). They are checked, generated and
        encoded like FKs (MATCH SIMPLE) but never declared to an engine."""
        self.ddl, self.dialect = ddl, dialect
        self.tables: dict[str, Table] = {}
        for st in sqlglot.parse(ddl, read=dialect):
            if isinstance(st, exp.Create) and isinstance(st.this, exp.Schema) and (st.args.get("kind") or "").upper() == "TABLE":
                self._table(st.this)
        for t, cols, ref, rcols in inclusions:
            self.tables[t.lower()].fks.append(FK([c.lower() for c in cols], ref.lower(), [c.lower() for c in rcols],
                                                 ddl=False))
        for t in self.tables.values():
            for fk in t.fks:
                if fk.ref not in self.tables:
                    raise ValueError(f"FK {t.name}{fk.cols} references unknown table {fk.ref}")
                if not fk.ref_cols:
                    fk.ref_cols = list(self.tables[fk.ref].pk)
                assert len(fk.ref_cols) == len(fk.cols), f"FK arity mismatch in {t.name}"
            for c in t.pk:
                t.col(c).nullable = False
        self.order = self._topo()
        self._index()

    # ------------------------------------------------------------------ parsing
    def _table(self, sch: exp.Schema):
        name = sch.this.name.lower()
        t = Table(name, [])
        pending_checks = []
        for e in sch.expressions:
            if isinstance(e, exp.ColumnDef):
                kind = e.args.get("kind")
                ty, param = map_type(kind) if kind is not None else (STR, None)
                col = Column(e.name.lower(), ty, True, kind.sql() if kind is not None else "TEXT", param)
                t.columns.append(col)
                for cc in e.args.get("constraints") or []:
                    k = cc.args.get("kind")
                    if isinstance(k, exp.NotNullColumnConstraint) and not k.args.get("allow_null"):
                        col.nullable = False
                    elif isinstance(k, exp.PrimaryKeyColumnConstraint):
                        t.pk = [col.name]
                    elif isinstance(k, exp.UniqueColumnConstraint):
                        t.uniques.append([col.name])
                    elif isinstance(k, exp.Reference):
                        ref = k.this
                        rt = ref.this.name.lower() if isinstance(ref, exp.Schema) else ref.name.lower()
                        rc = [_ident(x) for x in ref.expressions] if isinstance(ref, exp.Schema) else []
                        t.fks.append(FK([col.name], rt, rc))
                    elif isinstance(k, exp.CheckColumnConstraint):
                        pending_checks.append(k.this)
            else:
                self._table_constraint(t, e, pending_checks)
        for c in pending_checks:
            cols = sorted({x.name.lower() for x in c.find_all(exp.Column)})
            t.checks.append(Check(c, cols))
        self.tables[name] = t

    def _table_constraint(self, t, e, pending_checks):
        if isinstance(e, exp.Constraint):
            for x in e.expressions:
                self._table_constraint(t, x, pending_checks)
        elif isinstance(e, exp.PrimaryKey):
            t.pk = [_ident(x) for x in e.expressions]
        elif isinstance(e, exp.UniqueColumnConstraint):
            this = e.this
            cols = [_ident(x) for x in this.expressions] if isinstance(this, exp.Schema) else []
            if cols:
                t.uniques.append(cols)
        elif isinstance(e, exp.ForeignKey):
            ref = e.args["reference"].this
            rt = ref.this.name.lower() if isinstance(ref, exp.Schema) else ref.name.lower()
            rc = [_ident(x) for x in ref.expressions] if isinstance(ref, exp.Schema) else []
            t.fks.append(FK([_ident(x) for x in e.expressions], rt, rc))
        elif isinstance(e, exp.CheckColumnConstraint):
            pending_checks.append(e.this)
        elif isinstance(e, exp.Check):
            pending_checks.append(e.this)

    def _topo(self):
        """Topological order (parents first). A cycle is broken at an FK all of whose columns are nullable."""
        deps = {n: {fk.ref for fk in t.fks if fk.ref != n} for n, t in self.tables.items()}
        order, done = [], set()
        while len(order) < len(self.tables):
            ready = [n for n in self.tables if n not in done and deps[n] <= done]
            if ready:
                for n in ready:          # keep DDL order among ready tables
                    order.append(n); done.add(n)
                continue
            # cycle: defer an undeclared inclusion or a nullable declared FK
            broke = False
            for n in self.tables:
                if n in done:
                    continue
                for fk in self.tables[n].fks:
                    if fk.ref not in done and not fk.deferred and (not fk.ddl or all(self.tables[n].col(c).nullable for c in fk.cols)):
                        fk.deferred = True
                        deps[n] = {f.ref for f in self.tables[n].fks if not f.deferred and f.ref != n}
                        broke = True
                        break
                if broke:
                    break
            if not broke:
                raise ValueError("FK cycle without a nullable column; cannot order tables")
        # self references are always deferred (parent row must already exist)
        for t in self.tables.values():
            for fk in t.fks:
                if fk.ref == t.name:
                    fk.deferred = True
        return order

    def _index(self):
        self.coltab = {}                # unqualified column name -> [tables]
        for t in self.tables.values():
            for c in t.columns:
                self.coltab.setdefault(c.name, []).append(t.name)
        self.children = {n: [] for n in self.tables}   # parent -> [(child, fk)]
        for t in self.tables.values():
            for fk in t.fks:
                self.children[fk.ref].append((t.name, fk))
        self.keycols = {}               # table -> set of columns whose equality matters (PK, UNIQUE, FK both sides)
        for t in self.tables.values():
            s = set(t.pk) | {c for u in t.uniques for c in u} | {c for fk in t.fks for c in fk.cols}
            self.keycols.setdefault(t.name, set()).update(s)
        for t in self.tables.values():
            for fk in t.fks:
                self.keycols.setdefault(fk.ref, set()).update(fk.ref_cols)
        # key domains: union-find over FK column pairs, so that equal codes mean equal values across tables
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for t in self.tables.values():
            for c in self.keycols[t.name]:
                find((t.name, c))
            for fk in t.fks:
                for a, b in zip(fk.cols, fk.ref_cols):
                    ra, rb = find((t.name, a)), find((fk.ref, b))
                    if ra != rb:
                        parent[ra] = rb
        self.domain = {k: find(k) for k in list(parent)}

    # ------------------------------------------------------------------ helpers
    def is_key(self, t, c):
        return c in self.keycols.get(t, ())

    def column(self, t, c):
        return self.tables[t].col(c)

    def fk_tuple(self, row, fk):
        return tuple(row[c] for c in fk.cols)

    def ddl_for(self, dialect: str, deferred_fk=True):
        """CREATE TABLE statements for a backend, constraints declared (so the engine double-checks instances)."""
        out = []
        for n in self.order:
            t = self.tables[n]
            q = (lambda x: "`" + x.replace("`", "``") + "`") if dialect == "mysql" else qid
            parts = [f"{q(c.name)} {c.raw}" + ("" if c.nullable else " NOT NULL") for c in t.columns]
            if t.pk:
                parts.append(f"PRIMARY KEY ({', '.join(map(q, t.pk))})")
            for u in t.uniques:
                parts.append(f"UNIQUE ({', '.join(map(q, u))})")
            for fk in t.fks:
                if (fk.deferred and not deferred_fk) or not fk.ddl:
                    continue
                s = f"FOREIGN KEY ({', '.join(map(q, fk.cols))}) REFERENCES {q(fk.ref)} ({', '.join(map(q, fk.ref_cols))})"
                if fk.deferred and dialect == "postgres":
                    s += " DEFERRABLE INITIALLY DEFERRED"
                parts.append(s)
            for ch in t.checks:
                parts.append(f"CHECK ({ch.expr.sql(dialect=dialect)})")
            sql = f"CREATE TABLE {q(n)} ({', '.join(parts)})"
            # MySQL: emitted directly (backtick identifiers, the declared column types kept verbatim)
            out.append(sqlglot.transpile(sql, read="duckdb", write=dialect)[0] if dialect not in ("duckdb", "mysql")
                       else sql)
        return out

    # ------------------------------------------------------------------ constraint checking (runner assertion)
    def violations(self, db):
        """List of violated constraints of db = {table: [row dict]}; [] iff db satisfies the declared constraints."""
        bad = []
        for n in self.order:
            t = self.tables[n]
            rows = db.get(n, [])
            for r in rows:
                for c in t.columns:
                    if c.name not in r:
                        bad.append(f"{n}: missing column {c.name}")
                    elif r[c.name] is None and not c.nullable:
                        bad.append(f"{n}.{c.name} NOT NULL")
            for key in ([t.pk] if t.pk else []) + t.uniques:
                seen = set()
                for r in rows:
                    k = tuple(r.get(c) for c in key)
                    if any(v is None for v in k):
                        continue     # PK columns are NOT NULL (checked above); UNIQUE: NULLs are distinct
                    if k in seen:
                        bad.append(f"{n}{tuple(key)} duplicate {k}")
                    seen.add(k)
            for fk in t.fks:
                pk = {tuple(p.get(c) for c in fk.ref_cols) for p in db.get(fk.ref, [])}
                for r in rows:
                    k = tuple(r.get(c) for c in fk.cols)
                    if any(v is None for v in k):
                        continue     # MATCH SIMPLE
                    if k not in pk:
                        bad.append(f"{n}{tuple(fk.cols)} -> {fk.ref}: dangling {k}")
            for ch in t.checks:
                for r in rows:
                    if eval_check(ch.expr, r) is False:
                        bad.append(f"{n} CHECK ({ch.expr.sql()}) violated by {r}")
        return bad

    def check(self, db):
        v = self.violations(db)
        assert not v, "constraint violation: " + "; ".join(v[:5])
        return True


# ---------------------------------------------------------------------- CHECK evaluation (3VL; NULL passes)
def _lit(e, like):
    if isinstance(e, exp.Neg):
        x = _lit(e.this, like)
        return -x if x is not None else None
    if isinstance(e, exp.Paren):
        return _lit(e.this, like)
    if isinstance(e, exp.Cast):
        return _lit(e.this, like)
    if isinstance(e, exp.Null):
        return None
    if isinstance(e, exp.Boolean):
        return e.this
    if isinstance(e, exp.Literal):
        v = e.this
        if e.is_string:
            if isinstance(like, dt.date):
                try:
                    return dt.date.fromisoformat(v)
                except ValueError:
                    return v
            return v
        d = decimal.Decimal(v)
        return int(d) if d == int(d) and "." not in v else d
    raise ValueError(f"unsupported CHECK operand {e.sql()}")


def _val(e, row):
    if isinstance(e, exp.Column):
        return row.get(e.name.lower())
    if isinstance(e, exp.Paren):
        return _val(e.this, row)
    return _lit(e, None)


def _cmp(a, b, op):
    if a is None or b is None:
        return None
    if isinstance(a, dt.date) and isinstance(b, str):
        b = dt.date.fromisoformat(b)
    if isinstance(b, dt.date) and isinstance(a, str):
        a = dt.date.fromisoformat(a)
    a, b = (int(x) if isinstance(x, bool) else x for x in (a, b))     # BOOLEAN is 0/1 in every CHECK / enum
    if isinstance(a, decimal.Decimal) or isinstance(b, decimal.Decimal):
        if isinstance(a, (int, decimal.Decimal, float)) and isinstance(b, (int, decimal.Decimal, float)):
            a, b = decimal.Decimal(str(a)), decimal.Decimal(str(b))
    # SQLite storage-class order: NULL is handled above, then numeric, TEXT,
    # and BLOB. Keep numbers together so integer and real values compare by
    # value. Compute only the requested operator (the old dict was eager).
    def sqlite_key(value):
        if isinstance(value, (int, decimal.Decimal, float)):
            return (0, value)
        if isinstance(value, str):
            return (1, value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return (2, bytes(value))
        # Preserve existing comparisons for supported temporal values.
        if isinstance(value, (dt.date, dt.time, dt.datetime)):
            return (1, value.isoformat())
        raise TypeError(f"unsupported CHECK value: {type(value).__name__}")
    import operator
    compare = {"eq": operator.eq, "neq": operator.ne, "lt": operator.lt,
               "lte": operator.le, "gt": operator.gt, "gte": operator.ge}[op]
    return compare(sqlite_key(a), sqlite_key(b))


def eval_check(e, row):
    """Kleene evaluation of a simple CHECK predicate; None = unknown (which satisfies a CHECK)."""
    if isinstance(e, exp.Paren):
        return eval_check(e.this, row)
    if isinstance(e, exp.And):
        a, b = eval_check(e.this, row), eval_check(e.expression, row)
        if a is False or b is False:
            return False
        return None if a is None or b is None else True
    if isinstance(e, exp.Or):
        a, b = eval_check(e.this, row), eval_check(e.expression, row)
        if a is True or b is True:
            return True
        return None if a is None or b is None else False
    if isinstance(e, exp.Not):
        a = eval_check(e.this, row)
        return None if a is None else not a
    ops = {exp.EQ: "eq", exp.NEQ: "neq", exp.LT: "lt", exp.LTE: "lte", exp.GT: "gt", exp.GTE: "gte"}
    for k, op in ops.items():
        if isinstance(e, k):
            a = _val(e.this, row)
            b = _val(e.expression, row)
            return _cmp(a, b, op)
    if isinstance(e, exp.Between):
        x = _val(e.this, row)
        lo, hi = _val(e.args["low"], row), _val(e.args["high"], row)
        a, b = _cmp(x, lo, "gte"), _cmp(x, hi, "lte")
        if a is False or b is False:
            return False
        return None if a is None or b is None else True
    if isinstance(e, exp.In):
        x = _val(e.this, row)
        if x is None:
            return None
        vals = [_val(v, row) for v in e.expressions]
        if any(_cmp(x, v, "eq") for v in vals if v is not None):
            return True
        return None if any(v is None for v in vals) else False
    if isinstance(e, exp.Is):
        x = _val(e.this, row)
        return x is None
    if isinstance(e, exp.Boolean):
        return e.this
    if isinstance(e, exp.Null):
        return None
    raise ValueError(f"unsupported CHECK {e.sql()}")
