"""Normalized schema with integrity constraints, shared by every dataset and tool adapter.

One JSON file per schema (`schema_path` in a batch):
  {"name", "source",
   "tables": [{"name", "columns": [{"name", "type", "not_null"}], "primary_key": [..],
               "unique": [[..], ..], "foreign_keys": [{"columns": [..], "ref_table", "ref_columns": [..]}]}],
   "checks": [{"table", "expr"}],          # single-table predicates, e.g. Sales.price > 0 (SQL, unqualified)
   "inclusions": [{"columns", "table", "ref_table", "ref_columns"}],   # col_dep to a non-key column (no SQL FK)
   "enums": [{"table", "column", "values"}], "domains": [{"table", "column", "lo", "hi"}],
   "unsupported": ["<native constraint text we do not model>"],
   "native": {"sqlwitness": {"tables": [...], "constraints": "..."}}}   # original tool-format input, if any

Types are normalized to: int, real, numeric, varchar, date, timestamp, time, boolean.
Foreign keys follow SQL MATCH SIMPLE: a row with any NULL in the FK columns is not checked.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

NTYPES = ("int", "real", "numeric", "varchar", "date", "timestamp", "time", "boolean")


def norm_type(t: str) -> str:
    t = (t or "").lower().strip()
    if re.match(r"(big|small|tiny|medium)?int|integer|serial|identifier", t):
        return "int"
    if re.match(r"(decimal|numeric|money)", t):
        return "numeric"
    if re.match(r"(real|float|double)", t):
        return "real"
    if re.match(r"(bool)", t):
        return "boolean"
    if t.startswith("date") and "time" not in t:
        return "date"
    if re.match(r"(datetime|timestamp)", t):
        return "timestamp"
    if t.startswith("time"):
        return "time"
    return "varchar"


@dataclass
class Table:
    name: str
    columns: List[Dict[str, Any]]
    primary_key: List[str] = field(default_factory=list)
    unique: List[List[str]] = field(default_factory=list)
    foreign_keys: List[Dict[str, Any]] = field(default_factory=list)

    def col(self, name):
        for c in self.columns:
            if c["name"].lower() == name.lower():
                return c
        return None


@dataclass
class Schema:
    name: str
    tables: List[Table]
    source: str = ""
    checks: List[Dict[str, str]] = field(default_factory=list)
    inclusions: List[Dict[str, Any]] = field(default_factory=list)
    enums: List[Dict[str, Any]] = field(default_factory=list)
    domains: List[Dict[str, Any]] = field(default_factory=list)
    unsupported: List[str] = field(default_factory=list)
    native: Dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- io
    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Schema":
        d = dict(d)
        d["tables"] = [Table(**t) for t in d["tables"]]
        return cls(**d)

    @classmethod
    def load(cls, path: str) -> "Schema":
        with open(path) as f:
            return cls.from_json(json.load(f))

    def dump(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_json(), f, indent=1, default=str)

    # ---------------------------------------------------------------- helpers
    def table(self, name: str) -> Optional[Table]:
        for t in self.tables:
            if t.name.lower() == name.lower():
                return t
        return None

    def is_key(self, table: str, cols: List[str]) -> bool:
        t = self.table(table)
        if t is None:
            return False
        s = {c.lower() for c in cols}
        keys = ([t.primary_key] if t.primary_key else []) + t.unique
        return any({c.lower() for c in k} == s for k in keys)

    def composite_fks(self) -> List[tuple]:
        return [(t.name, fk) for t in self.tables for fk in t.foreign_keys if len(fk["columns"]) > 1]

    def topo_order(self) -> List[str]:
        """Parents before children (FK graph); cycles broken arbitrarily."""
        deps = {t.name: {fk["ref_table"] for fk in t.foreign_keys if fk["ref_table"] != t.name} for t in self.tables}
        out, seen = [], set()

        def visit(n, stack=()):
            if n in seen or n in stack:
                return
            for p in sorted(deps.get(n, ())):
                if p in deps:
                    visit(p, stack + (n,))
            seen.add(n)
            out.append(n)

        for t in self.tables:
            visit(t.name)
        return out

    def stats(self) -> Dict[str, int]:
        return {"tables": len(self.tables),
                "pk": sum(bool(t.primary_key) for t in self.tables),
                "composite_pk": sum(len(t.primary_key) > 1 for t in self.tables),
                "fk": sum(len(t.foreign_keys) for t in self.tables),
                "composite_fk": len(self.composite_fks()),
                "unique": sum(len(t.unique) for t in self.tables),
                "not_null": sum(bool(c.get("not_null")) for t in self.tables for c in t.columns),
                "checks": len(self.checks), "inclusions": len(self.inclusions),
                "enums": len(self.enums), "domains": len(self.domains), "unsupported": len(self.unsupported)}


# ====================================================================== DDL emitters
SQLTYPE = {
    "postgresql": {"int": "BIGINT", "real": "DOUBLE PRECISION", "numeric": "NUMERIC", "varchar": "TEXT", "date": "DATE",
                   "timestamp": "TIMESTAMP", "time": "TIME", "boolean": "BOOLEAN"},
    "sqlite": {"int": "INTEGER", "real": "REAL", "numeric": "NUMERIC", "varchar": "TEXT", "date": "DATE",
               "timestamp": "DATETIME", "time": "TIME", "boolean": "BOOLEAN"},
    "mysql": {"int": "BIGINT", "real": "DOUBLE", "numeric": "DECIMAL(30,10)", "varchar": "VARCHAR(128)", "date": "DATE",
              "timestamp": "DATETIME", "time": "TIME", "boolean": "BOOLEAN"},
    "duckdb": {"int": "BIGINT", "real": "DOUBLE", "numeric": "DECIMAL(18,4)", "varchar": "VARCHAR", "date": "DATE",
               "timestamp": "TIMESTAMP", "time": "TIME", "boolean": "BOOLEAN"},
}


def q_ident(name: str, dialect: str) -> str:
    # identifiers are lowercased everywhere, as SQLWitness does for both its DDL and the queries
    # (`_lowercase_sql_preserve_literals`); engines lowercase queries the same way (engine.normalize_sql)
    name = name.lower()
    if dialect == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def _type_sql(c, dialect):
    raw = c.get("sql_type")
    if raw and dialect in ("postgresql", "duckdb"):
        return raw
    return SQLTYPE[dialect][c["type"]]


def _lit(v, ty):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)) and ty in ("int", "real", "numeric", "boolean"):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def to_ddl(schema: Schema, dialect: str, *, with_fk=True, with_checks=True, deferrable=True, quote=True,
           bare=False) -> List[str]:
    """CREATE TABLE statements in topological order. FKs to non-key parents are not declarable in SQL; they are
    checked by the validator in Python (`inclusions` and such FKs). `bare`: no constraints at all (used to ask
    whether a constraint-violating counterexample would at least distinguish the queries)."""
    if bare:
        with_fk = with_checks = False
    qi = (lambda n: q_ident(n, dialect)) if quote else (lambda n: n)
    stmts = []
    for tn in schema.topo_order():
        t = schema.table(tn)
        parts = []
        for c in t.columns:
            nn = " NOT NULL" if (c.get("not_null") or (c["name"] in t.primary_key)) and not bare else ""
            parts.append(f"{qi(c['name'])} {_type_sql(c, dialect)}{nn}")
        if t.primary_key and not bare:
            parts.append("PRIMARY KEY (" + ", ".join(qi(c) for c in t.primary_key) + ")")
        for u in ([] if bare else t.unique):
            parts.append("UNIQUE (" + ", ".join(qi(c) for c in u) + ")")
        if with_fk:
            for fk in t.foreign_keys:
                if not schema.is_key(fk["ref_table"], fk["ref_columns"]):
                    continue
                pt = schema.table(fk["ref_table"])
                if any(t.col(c)["type"] != pt.col(pc)["type"] for c, pc in zip(fk["columns"], fk["ref_columns"])):
                    continue  # type-incompatible FK (engines reject it); checked in Python by the validator
                d = " DEFERRABLE INITIALLY DEFERRED" if deferrable and dialect in ("postgresql", "sqlite") else ""
                parts.append("FOREIGN KEY (" + ", ".join(qi(c) for c in fk["columns"]) + ") REFERENCES "
                             + qi(fk["ref_table"]) + " (" + ", ".join(qi(c) for c in fk["ref_columns"]) + ")" + d)
        if with_checks:
            for ck in schema.checks:
                if ck["table"].lower() == t.name.lower():
                    # check expressions write identifiers as "col"; MySQL reads "col" as a string literal
                    expr = re.sub(r'"([^"]+)"', lambda m: q_ident(m.group(1), dialect), ck["expr"])
                    parts.append(f"CHECK ({expr})")
            for e in schema.enums:  # SQLWitness/LeetCode `T.c <- {v, ...}`: NULL allowed, as the validator
                if e["table"].lower() == t.name.lower() and t.col(e["column"]):
                    vals = ", ".join(_lit(v, t.col(e["column"])["type"]) for v in e["values"])
                    parts.append(f"CHECK ({q_ident(t.col(e['column'])['name'], dialect)} IN ({vals}))")
            for d in schema.domains:  # `T.c <- [lo, hi]`
                if d["table"].lower() == t.name.lower() and t.col(d["column"]):
                    ty = t.col(d["column"])["type"]
                    parts.append(f"CHECK ({q_ident(t.col(d['column'])['name'], dialect)} BETWEEN "
                                 f"{_lit(d['lo'], ty)} AND {_lit(d['hi'], ty)})")
        stmts.append(f"CREATE TABLE {qi(t.name)} (" + ", ".join(parts) + ")")
    return stmts


# ====================================================================== SQLWitness / Polygon format
def from_sqlwitness(name: str, tables: List[Dict[str, Any]], constraints: Any, source: str = "") -> Schema:
    """SQLWitness/Polygon schema JSON (`TableName`, `PKeys`, `FKeys` with PTable index, `Others`) plus SQLWitness
    constraint text (or a pre-parsed list from its own Lark parser)."""
    out = []
    declared = {(t["TableName"], c["Name"]): c["Type"] for t in tables for c in t.get("PKeys", []) + t.get("Others", [])}
    for t in tables:
        cols = []
        pk = []
        for c in t.get("PKeys", []):
            cols.append({"name": c["Name"], "type": norm_type(c["Type"]), "not_null": True})
            pk.append(c["Name"])
        # SQLWitness column order is PKeys, FKeys, Others; an FK column listed only under FKeys takes the parent's type
        for fk in t.get("FKeys", []):
            if not any(x["name"] == fk["FName"] for x in cols):
                ptype = declared.get((tables[int(fk["PTable"])]["TableName"], fk["PName"]), "varchar")
                cols.append({"name": fk["FName"], "type": norm_type(ptype), "not_null": False})
        for c in t.get("Others", []):
            if not any(x["name"] == c["Name"] for x in cols):
                cols.append({"name": c["Name"], "type": norm_type(c["Type"]), "not_null": False})
        out.append(Table(t["TableName"], cols, pk, [], []))
    for t, raw in zip(out, tables):
        for fk in raw.get("FKeys", []):
            p = tables[int(fk["PTable"])]["TableName"]
            t.foreign_keys.append({"columns": [fk["FName"]], "ref_table": p, "ref_columns": [fk["PName"]]})
    s = Schema(name, out, source=source, native={"sqlwitness": {"tables": tables, "constraints": constraints}})
    if constraints:
        parsed = constraints if isinstance(constraints, list) else parse_sqlwitness_constraints(constraints)
        apply_parsed_constraints(s, parsed)
    # FK whose parent columns are not a key: keep as an inclusion dependency instead (not declarable in SQL)
    for t in s.tables:
        keep = []
        for fk in t.foreign_keys:
            if s.is_key(fk["ref_table"], fk["ref_columns"]):
                keep.append(fk)
            else:
                s.inclusions.append({"table": t.name, **fk})
        t.foreign_keys = keep
    return s


def _split_attr(a: str):
    tb, col = a.split(".", 1)
    return tb, col


def apply_parsed_constraints(s: Schema, parsed: List[Dict[str, Any]]) -> None:
    """Map SQLWitness ConstraintVisitor output onto the normalized schema."""
    for c in parsed:
        (k, v), = c.items()
        try:
            if k == "not_null":
                tb, col = _split_attr(v)
                t = s.table(tb)
                cc = t.col(col) if t else None
                if cc is None:
                    s.unsupported.append(json.dumps(c, default=str)); continue
                cc["not_null"] = True
            elif k == "distinct":
                attrs = [_split_attr(a) for a in v]
                tb = attrs[0][0]
                t = s.table(tb)
                if t is None or any(a[0].lower() != tb.lower() for a in attrs):
                    s.unsupported.append(json.dumps(c, default=str)); continue
                cols = [t.col(a[1])["name"] for a in attrs]
                if [x.lower() for x in cols] != [x.lower() for x in t.primary_key] and cols not in t.unique:
                    t.unique.append(cols)
            elif k == "membership":
                (ct, cc), (pt, pc) = _split_attr(v[0]), _split_attr(v[1])
                t = s.table(ct)
                if t is None or s.table(pt) is None:
                    s.unsupported.append(json.dumps(c, default=str)); continue
                fk = {"columns": [t.col(cc)["name"]], "ref_table": s.table(pt).name,
                      "ref_columns": [s.table(pt).col(pc)["name"]]}
                if fk not in t.foreign_keys:
                    t.foreign_keys.append(fk)
            elif k == "enum":
                tb, col = _split_attr(v[0])
                s.enums.append({"table": tb, "column": col, "values": v[1]})
            elif k == "domain":
                tb, col = _split_attr(v[0])
                s.domains.append({"table": tb, "column": col, "lo": v[1], "hi": v[2]})
            elif k in ("gt", "gte", "lt", "lte", "neq", "equal"):
                a, b = v
                op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "neq": "<>", "equal": "="}[k]

                def side(x):
                    if isinstance(x, str) and re.match(r"^[A-Za-z_]\w*\.[A-Za-z_]\w*$", x):
                        return _split_attr(x)
                    return None

                sa, sb = side(a), side(b)
                tabs = {x[0].lower() for x in (sa, sb) if x}
                if len(tabs) != 1:
                    s.unsupported.append(json.dumps(c, default=str)); continue

                def lit(x, sx):
                    if sx:
                        return '"' + sx[1] + '"'
                    if isinstance(x, (int, float)):
                        return repr(x)
                    if x is None:
                        return "NULL"
                    return "'" + str(x).replace("'", "''") + "'"

                tb = (sa or sb)[0]
                s.checks.append({"table": s.table(tb).name if s.table(tb) else tb,
                                 "expr": f"{lit(a, sa)} {op} {lit(b, sb)}"})
            else:  # inc / dec / consec / override / join
                s.unsupported.append(json.dumps(c, default=str))
        except Exception:
            s.unsupported.append(json.dumps(c, default=str))


# Grammar copied from SQLWitness (sqlwitness/online/constraints.py, Apache-2.0) so that datasets are parsed exactly
# as SQLWitness parses them.
_CONSTRAINT_GRAMMAR = r'''
constraints: constraint (";" constraint)* [";"]
?constraint : membership | inclusion | not_null | comparison | unique | increment | consec | dec | override | if_then
membership : attribute "<-" attribute | attribute "<-" value_range | attribute "<-" value_items
inclusion :  attribute "->" value_items | attribute "->" value_range
if_then:  value op value  "=>"  value op value
not_null : attribute "!=" NULL
comparison : value op value
override: attribute "|" "int+" NULL | attribute "|" "varchar+" NULL
unique : "unique" "(" [attribute ("," attribute)*] ")"
increment: "inc" "(" [attribute ("," attribute)*] ")"
dec: "dec" "(" [attribute ("," attribute)*] ")"
consec: "consec" "(" [attribute ("," attribute)*] ")"
?value : attribute | NUMBER | DATE | STRING | NULL
!op : ">" | "<" | "!=" | ">=" | "<=" | "^" | "="
attribute : /[^\W\d]\w*/ "." /[^\W\d]\w*/
value_range : "[" NUMBER "," NUMBER "]" | "[" DATE "," DATE "]"
value_items : "{" [value_item ("," value_item)*] "}"
?value_item : STRING | NUMBER | NULL
NULL: "null" | "NULL"
DATE: NUMBER "-" NUMBER "-" NUMBER
STRING : "'" /[^']+/ "'" | ESCAPED_STRING | /[^\W\d]\w*/
%import common.ESCAPED_STRING
%import common.SIGNED_NUMBER -> NUMBER
%import common.WS
%ignore WS
'''


def parse_sqlwitness_constraints(text: str) -> List[Dict[str, Any]]:
    """Parse SQLWitness constraint text, or a LeetCode-style .yml (row_dep/col_dep/col_val)."""
    import datetime
    from lark import Lark, Transformer

    class V(Transformer):
        def constraints(self, t): return [n for n in t if n is not None]

        def membership(self, t):
            if isinstance(t[1], str):
                return {"membership": t}
            if isinstance(t[1], tuple):
                return {"domain": [t[0], t[1][0], t[1][1]]}
            return {"enum": [str(t[0]), list(t[1])]}

        def inclusion(self, t):
            if isinstance(t[1], tuple):
                return {"domain": [t[0], t[1][0], t[1][1]]}
            return {"enum": [str(t[0]), list(t[1])]}

        def not_null(self, t):
            return {"not_null": t[0]}

        def if_then(self, t): return {"if_then": [str(x) for x in t]}
        def comparison(self, t): return {t[1]: [t[0], t[2]]}
        def unique(self, t): return {"distinct": list(t)}
        def increment(self, t): return {"inc": list(t)}
        def dec(self, t): return {"dec": list(t)}
        def consec(self, t): return {"consec": list(t)}
        def override(self, t): return {"override": t[0]}

        def op(self, t):
            return {">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "!=": "neq", "^": "join", "=": "equal"}[str(t[0])]

        def attribute(self, t): return f"{t[0]}.{t[1]}"
        def NUMBER(self, t): return eval(t)
        def null(self, t): return None
        def value_range(self, t): return t[0], t[1]
        def value_items(self, t): return t
        def DATE(self, t): return datetime.date.fromisoformat(t)
        def STRING(self, t): return str(t.value[1:-1]) if t.value[:1] in "'\"" else str(t.value)

    parser = Lark(_CONSTRAINT_GRAMMAR, start="constraints")
    text = text.strip()
    if not text:
        return []
    if re.match(r"^\s*(row_dep|col_dep|col_val)\s*:", text, re.M):
        import yaml
        d = yaml.safe_load(text)
        out = []
        for v in (d or {}).values():
            if v:
                out.extend(V().transform(parser.parse(str(v))))
        return out
    return V().transform(parser.parse(text))


def to_sqlwitness(schema: Schema) -> tuple:
    """(tables, constraint_text) in the SQLWitness/Polygon input format. Composite FKs and composite UNIQUEs are
    the tools' limitation: a composite FK becomes one single-column inclusion per column (weaker than the real
    constraint, exactly what these tools can express); composite PKs are kept as multi-column PKeys."""
    if "sqlwitness" in schema.native:
        n = schema.native["sqlwitness"]
        return n["tables"], n["constraints"]
    idx = {t.name: i for i, t in enumerate(schema.tables)}
    tables, cons = [], []
    for t in schema.tables:
        tys = {"int": "int", "real": "real", "numeric": "numeric", "varchar": "varchar", "date": "date",
               "timestamp": "datetime", "time": "varchar", "boolean": "boolean"}
        fks = []
        for fk in t.foreign_keys:
            for c, pc in zip(fk["columns"], fk["ref_columns"]):
                if len(fk["columns"]) == 1:
                    fks.append({"FName": c, "PName": pc, "PTable": str(idx[fk["ref_table"]])})
                else:
                    cons.append(f"{t.name}.{c} <- {fk['ref_table']}.{pc}")
        tables.append({"TableName": t.name,
                       "PKeys": [{"Name": c, "Type": tys[t.col(c)["type"]]} for c in t.primary_key],
                       "FKeys": fks,
                       "Others": [{"Name": c["name"], "Type": tys[c["type"]]} for c in t.columns
                                  if c["name"] not in t.primary_key]})
        for c in t.columns:
            if c.get("not_null") and c["name"] not in t.primary_key:
                cons.append(f"{t.name}.{c['name']} != NULL")
        for u in t.unique:
            cons.append("unique(" + ", ".join(f"{t.name}.{c}" for c in u) + ")")
    return tables, "; ".join(cons)


# ====================================================================== DDL parser (TPC-H / TPC-DS)
def from_ddl(name: str, ddl: str, dialect: str = "postgres", source: str = "") -> Schema:
    import sqlglot
    from sqlglot import exp
    tables: Dict[str, Table] = {}
    pending_fk, pending_ck = [], []
    for st in sqlglot.parse(ddl, read=dialect):
        if st is None:
            continue
        if isinstance(st, exp.Create) and st.kind == "TABLE":
            sch = st.this
            tn = sch.this.name
            t = Table(tn, [])
            for e in sch.expressions:
                if isinstance(e, exp.ColumnDef):
                    nn = any(isinstance(c.kind, exp.NotNullColumnConstraint) and not c.kind.args.get("allow_null")
                             for c in e.constraints or [])
                    raw = e.args["kind"].sql(dialect="postgres") if e.args.get("kind") else "TEXT"
                    t.columns.append({"name": e.name, "type": norm_type(raw), "sql_type": raw, "not_null": nn})
                    for c in e.constraints or []:
                        if isinstance(c.kind, exp.PrimaryKeyColumnConstraint):
                            t.primary_key = [e.name]
                        elif isinstance(c.kind, exp.CheckColumnConstraint):
                            pending_ck.append((tn, c.kind.this))
                        elif isinstance(c.kind, exp.UniqueColumnConstraint):
                            t.unique.append([e.name])
                        elif isinstance(c.kind, exp.Reference):
                            ref = c.kind.this
                            pending_fk.append((tn, [e.name], ref.this.name,
                                               [x.name for x in ref.expressions]))
                elif isinstance(e, exp.PrimaryKey):
                    t.primary_key = [x.name for x in e.expressions]
                elif isinstance(e, exp.ForeignKey):
                    ref = e.args["reference"].this
                    pending_fk.append((tn, [x.name for x in e.expressions], ref.this.name,
                                       [x.name for x in ref.expressions]))
                elif isinstance(e, exp.UniqueColumnConstraint):
                    t.unique.append([x.name for x in e.this.expressions])
                elif isinstance(e, exp.Constraint) or isinstance(e, exp.CheckColumnConstraint):
                    for k in (e.expressions if isinstance(e, exp.Constraint) else [e]):
                        if isinstance(k, exp.PrimaryKey):
                            t.primary_key = [x.name for x in k.expressions]
                        elif isinstance(k, exp.ForeignKey):
                            ref = k.args["reference"].this
                            pending_fk.append((tn, [x.name for x in k.expressions], ref.this.name,
                                               [x.name for x in ref.expressions]))
                        elif isinstance(k, exp.UniqueColumnConstraint):
                            t.unique.append([x.name for x in k.this.expressions])
                        elif isinstance(k, exp.CheckColumnConstraint):
                            pending_ck.append((tn, k.this))
            tables[tn] = t
        elif isinstance(st, exp.Alter):
            tn = st.this.name
            for a in st.args.get("actions", []):
                fk = a.find(exp.ForeignKey)
                if fk is not None:
                    ref = fk.args["reference"].this
                    pending_fk.append((tn, [x.name for x in fk.expressions], ref.this.name,
                                       [x.name for x in ref.expressions]))
                pk = a.find(exp.PrimaryKey)
                if pk is not None and fk is None:
                    tables[tn].primary_key = [x.name for x in pk.expressions]
    for tn, cols, rt, rc in pending_fk:
        fk = {"columns": cols, "ref_table": rt, "ref_columns": rc}
        if fk not in tables[tn].foreign_keys:
            tables[tn].foreign_keys.append(fk)
    for t in tables.values():
        for c in t.columns:
            if c["name"] in t.primary_key:
                c["not_null"] = True
    s = Schema(name, list(tables.values()), source=source)
    for tn, e in pending_ck:  # CHECK: one entry per conjunct; `col IN (literals)` becomes an enum
        while isinstance(e, exp.Paren):
            e = e.this
        for conj in (e.flatten() if isinstance(e, exp.And) else [e]):
            while isinstance(conj, exp.Paren):
                conj = conj.this
            if isinstance(conj, exp.In) and isinstance(conj.this, exp.Column) and conj.expressions \
                    and all(isinstance(x, exp.Literal) for x in conj.expressions):
                vals = [x.this if x.is_string else (int(x.this) if re.match(r"^-?\d+$", x.this) else float(x.this))
                        for x in conj.expressions]
                s.enums.append({"table": tn, "column": conj.this.name, "values": vals})
            else:
                s.checks.append({"table": tn, "expr": conj.sql(dialect="postgres")})
    return s
