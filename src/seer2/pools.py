"""Per-column value pools and key domains.

Pool of a non-key column: type seeds (two strings / dates, a spread of numbers), NULL if the column is nullable, literals of the batch compared with
that column (or, if a literal is not attached to a column, every column of its type) with boundary neighbours
(+-1, +-0.01 at the column scale, dates +-1 day and month/year boundaries, LIKE patterns turned into matching and
non-matching strings), and optionally a few values sampled from a source database. Values violating a single-column
CHECK are removed.

Key columns (PK, UNIQUE, FK on either side) live in key *domains*: FK-connected columns share one domain so that
equal integer codes mean equal values in every table. Integer domains use the value itself as code; other domains
map codes < OFFSET to explicit values and mint fresh values for codes >= OFFSET.
"""
from __future__ import annotations

import datetime as dt
import decimal
import re

import sqlglot
from sqlglot import exp

from .schema import BOOL, DATE, DEC, FLOAT, INT, STR, TS, Schema, eval_check

OFFSET = 1_000_000          # first minted code of a non-integer key domain
FRESH_SPAN = 1_000_000      # width of the fresh-code region of a key domain
MAX_POOL = 40
NULLK = -(2 ** 31) + 7      # code of NULL in a nullable key column

# numeric seeds are spread out (not only 1, 2) so that SUM / AVG / products over witness rows are not degenerate
SEEDS = {INT: [1, 2, 3, 7, 40], DEC: [decimal.Decimal(x) for x in ("1", "2", "3", "0.05", "7.5", "40.25")],
         FLOAT: [1.0, 2.0, 3.0, 0.05, 7.5, 40.25], STR: ["a", "b"],
         DATE: [dt.date(2000, 1, 1), dt.date(2000, 1, 2)], BOOL: [True, False],
         TS: [dt.datetime(2000, 1, 1), dt.datetime(2000, 1, 2)]}

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")


def _strlen(raw):
    m = re.search(r"CHAR\w*\s*\((\d+)\)", raw, re.I)
    return int(m.group(1)) if m else None


def dec_limit(param):
    """Exclusive bound on |v| of a DECIMAL(p, s) column: 10^(p-s) (no bound when the precision is not declared)."""
    if not param or param[0] is None:
        return decimal.Decimal(10) ** 30
    return decimal.Decimal(10) ** (param[0] - (param[1] or 0))


def coerce(v, ty, param=None):
    """Convert a literal-derived Python value to the column type (None if impossible)."""
    if v is None:
        return None
    try:
        if ty == INT:
            d = decimal.Decimal(str(v))
            return int(d) if d == d.to_integral_value() else None
        if ty == DEC:
            scale = param[1] if param else 2
            q = decimal.Decimal(1).scaleb(-scale)
            d = decimal.Decimal(str(v)).quantize(q)
            return d if abs(d) < dec_limit(param) else None
        if ty == FLOAT:
            return float(v)
        if ty == DATE:
            if isinstance(v, dt.datetime):
                return v.date()
            if isinstance(v, dt.date):
                return v
            return dt.date.fromisoformat(str(v)) if _DATE_RE.match(str(v)) else None
        if ty == TS:
            if isinstance(v, dt.datetime):
                return v
            if isinstance(v, dt.date):
                return dt.datetime(v.year, v.month, v.day)
            return dt.datetime.fromisoformat(str(v))
        if ty == BOOL:
            return bool(v) if isinstance(v, bool) else None
        return v if isinstance(v, str) else str(v)
    except Exception:
        return None


def _date_neighbours(d0):
    out = set()
    for delta in (0, -1, 1, 30, -30, 31, 90, 91, -90, 365, -365, 366, 89, 92, 364):
        try:
            out.add(d0 + dt.timedelta(days=delta))
        except OverflowError:       # literals at the ends of the date range (0001-01-01, 9999-12-31)
            pass
    for mm in (1, 3, 12):
        m = d0.month - 1 + mm
        try:
            nxt = d0.replace(year=d0.year + m // 12, month=m % 12 + 1)
            out.update({nxt, nxt - dt.timedelta(days=1)})
        except ValueError:
            pass
    return out


def _like_strings(pat):
    core = pat.replace("%", " ").replace("_", "a")
    return {core.strip(), core, "x" + core + "x", pat.replace("%", ""), pat.replace("%", "").upper(),
            "zz" + pat.replace("%", "")[1:]}


def _num_neighbours(x):
    return {x + decimal.Decimal(d) for d in ("0", "1", "-1", "0.01", "-0.01")}


def _iso(x):
    return x.isoformat(sep=" ") if isinstance(x, dt.datetime) else x.isoformat()


_DATE_FUNCS = tuple(getattr(exp, n) for n in ("Date", "TsOrDsToDate", "TsOrDsToTimestamp", "StrToDate", "StrToTime",
                                              "TimeStrToTime", "DateStrToDate", "TimeStrToDate") if hasattr(exp, n))
_CASE_FUNCS = (exp.Lower, exp.Upper)
_PARTS = {getattr(exp, n): f for n, f in (("Year", "%Y"), ("Month", "%m"), ("Day", "%d")) if hasattr(exp, n)}
_INVERTIBLE = _DATE_FUNCS + _CASE_FUNCS + tuple(_PARTS) + (exp.TimeToStr, exp.Substring, exp.Trim)
_FMT_RE = re.compile(r"%[YmdHMSjw]")


def _dates_matching(fmt, v):
    """Dates / datetimes whose STRFTIME(fmt) is the string v (start, middle and end of the period it fixes)."""
    rx, fields = "", []
    i = 0
    while i < len(fmt):
        if fmt[i] == "%" and i + 1 < len(fmt):
            f = fmt[i + 1]
            rx += {"Y": r"(\d{4})", "j": r"(\d{3})"}.get(f, r"(\d{2})")
            fields.append(f)
            i += 2
        else:
            rx += re.escape(fmt[i])
            i += 1
    m = re.fullmatch(rx, str(v))
    if not m:
        return set()
    got = dict(zip(fields, map(int, m.groups())))
    out = set()
    for mo, d in ((1, 1), (6, 15), (12, 28)):
        try:
            x = dt.datetime(got.get("Y", 2000), got.get("m", mo), got.get("d", d), got.get("H", 12),
                            got.get("M", 0), got.get("S", 0))
        except ValueError:
            continue
        out |= {x, x.date()}
    return out


def invert(fn, vals):
    """Column values for which the scalar function fn (wrapped around the column) yields the given literal values:
    the literals' preimages, so a literal compared with STRFTIME('%Y', d), YEAR(d), DATE(d), SUBSTR(d, 1, n),
    LOWER(s), TRIM(s) puts matching values into d's / s's pool."""
    out = set()
    if isinstance(fn, exp.TimeToStr) or type(fn) in _PARTS:
        fmt = _PARTS.get(type(fn)) or (fn.args.get("format").this if isinstance(fn.args.get("format"), exp.Literal)
                                       else None)
        if not fmt or not _FMT_RE.search(fmt):
            return set()
        for v in vals:
            sv = str(v)
            if type(fn) in _PARTS:
                try:
                    sv = f"{int(decimal.Decimal(sv)):0{4 if fmt == '%Y' else 2}d}"
                except Exception:
                    continue
            out |= _dates_matching(fmt, sv)
        return out | {_iso(x) for x in out}
    if isinstance(fn, _DATE_FUNCS):
        for v in vals:
            if isinstance(v, dt.date):
                d0 = v if isinstance(v, dt.datetime) else dt.datetime(v.year, v.month, v.day)
                out |= {v, _iso(v), _iso(d0), _iso(d0.replace(hour=12))}
            else:
                out.add(v)
        return out
    if isinstance(fn, exp.Substring):
        st = fn.args.get("start")
        k = int(st.this) if isinstance(st, exp.Literal) and not st.is_string else 1
        for v in vals:
            sv = _iso(v) if isinstance(v, dt.date) else str(v)
            pre = "0" * max(0, k - 1)
            out |= {pre + sv, pre + sv + "x"}
            if re.fullmatch(r"\d{4}", sv) and k == 1:
                out |= {sv + "-01-01", sv + "-06-15 12:00:00"} | _dates_matching("%Y", sv)
            if _DATE_RE.match(sv) and k == 1:
                out |= {sv + " 12:00:00"} | ({v} if isinstance(v, dt.date) else set())
        return out
    if isinstance(fn, _CASE_FUNCS):
        for v in vals:
            if isinstance(v, str):
                out |= {v, v.upper(), v.lower(), v.capitalize()}
            else:
                out.add(v)
        return out
    if isinstance(fn, exp.Trim):
        for v in vals:
            out |= {v, " " + v, v + " "} if isinstance(v, str) else {v}
        return out
    return set(vals)


def literal_values(lit: exp.Literal):
    """Values derived from one literal: itself plus boundary neighbours (as Python values)."""
    v = lit.this
    if lit.is_string:
        if _DATE_RE.match(v):
            try:
                return _date_neighbours(dt.date.fromisoformat(v))
            except ValueError:
                return {v}
        return {v}
    try:
        return _num_neighbours(decimal.Decimal(v))
    except Exception:
        return set()


class Pools:
    def __init__(self, schema: Schema, sqls, dialect="duckdb", source=None, rng=None, max_pool=MAX_POOL):
        """source: optional {table: [row dict]} sample of a real database (a few values per column are added)."""
        self.schema = schema
        attached = {}          # (table, col) -> set of values
        exact = {}             # (table, col) -> literal strings as written (their derived values rank first)
        loose = []             # values not attached to any column
        likes = {}
        for sql in sqls:
            try:
                tree = sqlglot.parse_one(sql, read=dialect)
            except Exception:
                continue
            for lit in tree.find_all(exp.Literal):
                if isinstance(lit.parent, exp.TimeToStr) and lit.arg_key == "format":
                    continue                    # a format string, not a value
                vals = literal_values(lit)
                cols, wraps = self._attached_cols(lit)
                if isinstance(lit.parent, (exp.Like, exp.ILike)) and lit.is_string:
                    vals = _like_strings(lit.this) | {lit.this}
                for w in wraps:                 # outermost first: invert each scalar function down to the column
                    vals = invert(w, vals)
                if cols:
                    for c in cols:
                        attached.setdefault(c, set()).update(vals)
                        if lit.is_string:
                            exact.setdefault(c, set()).add(lit.this)
                else:
                    loose.extend(vals)
        # constants of Σ: the literals of each single-column CHECK (IN lists, comparison and BETWEEN bounds, with
        # their boundary neighbours), so a column whose CHECK admits no query constant and no type seed still has
        # values (e.g. semester IN ('Fall', ...), year > 1701 AND year < 2100)
        sigma = {}
        for tn in schema.order:
            for ch in schema.tables[tn].checks:
                if len(ch.cols) == 1:
                    for lit in ch.expr.find_all(exp.Literal):
                        sigma.setdefault((tn, ch.cols[0]), set()).update(literal_values(lit))
        self.col = {}
        for tn in schema.order:
            t = schema.tables[tn]
            for c in t.columns:
                cand = list(SEEDS[c.type])
                att = attached.get((tn, c.name), set())
                if c.type == STR:
                    # dates kept as text (SQLite, BIRD): a boundary date also with a time of day, which compares
                    # past the date as a string but equal to it after date()/SUBSTR(.., 1, 10)
                    att = att | {_iso(x) + " 12:00:00" for x in att
                                 if isinstance(x, dt.date) and not isinstance(x, dt.datetime)}
                ex = exact.get((tn, c.name), ())

                def rank(x, ex=ex):
                    sx = _iso(x) if isinstance(x, dt.date) else str(x)
                    return (0 if any(e and e in sx for e in ex) else 1, repr(x))
                cand += sorted(att, key=rank)
                cand += sorted(sigma.get((tn, c.name), ()), key=repr)
                lim = 12 if (tn, c.name) in attached else 24
                ls = [x for x in loose if coerce(x, c.type, c.param) is not None]
                cand += sorted(set(ls), key=repr)[:lim]
                if source and source.get(tn):
                    rows = source[tn]
                    sample = rng.sample(rows, min(len(rows), 50)) if rng else rows[:50]
                    cand += list(dict.fromkeys(r.get(c.name) for r in sample if r.get(c.name) is not None))[:4]
                vals = []
                maxlen = _strlen(c.raw) if c.type == STR else None
                for x in cand:
                    y = coerce(x, c.type, c.param)
                    if y is None or y in vals:
                        continue
                    if maxlen is not None and len(y) > maxlen:
                        continue
                    if any(ch.cols == [c.name] and eval_check(ch.expr, {c.name: y}) is False for ch in t.checks):
                        continue
                    vals.append(y)
                if len(vals) > max_pool:
                    vals = vals[:max_pool]
                if c.nullable:
                    vals.append(None)
                self.col[(tn, c.name)] = vals
        self.domains = {}
        for (tn, cn), root in schema.domain.items():
            if root not in self.domains:
                rc = schema.column(*root)
                self.domains[root] = Domain(root, rc.type, rc.param)
            d = self.domains[root]
            d.cols.add((tn, cn))
            keep = []
            for v in self.col[(tn, cn)]:
                # a column joined by an FK / inclusion to a key of another type (SQLite, undeclared inclusions):
                # a value not representable in the key's domain can never match a key value, so it is no candidate
                try:
                    if v is not None:
                        d.literal(v)
                    keep.append(v)
                except (ValueError, TypeError, decimal.InvalidOperation):
                    pass
            if all(v is None for v in keep) and d.is_int:     # every pool value was unrepresentable
                keep.insert(0, min(d.lits) if d.lits else 0)
            self.col[(tn, cn)] = keep

    def _attached_cols(self, lit):
        """Base columns a literal is compared with (by unqualified name; all tables having that column name), and
        the scalar functions wrapped around the column on the way (outermost first), e.g. STRFTIME('%Y', d) = '1997'
        attaches '1997' to d through the strftime wrapper."""
        p = lit.parent
        while isinstance(p, (exp.Paren, exp.Cast, exp.Neg, exp.Tuple)):
            p = p.parent
        if isinstance(p, (exp.Add, exp.Sub, exp.Mul, exp.Div)) or isinstance(p, exp.Interval):
            return [], []
        if not isinstance(p, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.Like,
                              exp.ILike)):
            return [], []
        col = p.this
        other = p.expression if isinstance(p, exp.Binary) and p.this is lit else None
        if other is not None:
            col = other
        wraps = []
        while not isinstance(col, exp.Column):
            if isinstance(col, (exp.Paren, exp.Cast, exp.TryCast)):
                col = col.this
            elif isinstance(col, _INVERTIBLE):
                wraps.append(col)
                col = col.this
            else:
                return [], []
        return [(t, col.name.lower()) for t in self.schema.coltab.get(col.name.lower(), [])], wraps

    def domain_of(self, t, c):
        return self.domains[self.schema.domain[(t, c)]]


class Domain:
    """Integer codes for the values of FK-connected key columns."""

    def __init__(self, root, ty, param):
        self.root, self.type, self.param = root, ty, param
        self.cols = set()
        self.values = []           # explicit values (non-int domains): code = index
        self.index = {}
        self.lits = set()          # literal-derived candidate values (as codes)
        self.minted = {}

    @property
    def is_int(self):
        return self.type == INT

    def literal(self, v):
        self.lits.add(self.encode(v))

    def encode(self, v):
        if v is None:
            return NULLK
        if self.is_int:
            return int(v)
        if v in self.index:
            return self.index[v]
        if v in self.minted:
            return self.minted[v]
        code = len(self.values)
        assert code < OFFSET
        self.values.append(v)
        self.index[v] = code
        return code

    def decode(self, code):
        if code == NULLK:
            return None
        if self.is_int:
            return code
        if code < OFFSET:
            return self.values[code]
        k = code - OFFSET
        if self.type == STR:
            v = f"~k{k}"
        elif self.type == DATE:
            v = dt.date(2100, 1, 1) + dt.timedelta(days=k)
        elif self.type == TS:
            v = dt.datetime(2100, 1, 1) + dt.timedelta(seconds=k)
        elif self.type == DEC:
            # fresh keys count down from the largest value the column's precision can hold (DECIMAL(5,2): 999.99,
            # 999.98, ...), so a minted key never overflows the declared type
            scale = self.param[1] if self.param else 0
            step = decimal.Decimal(1).scaleb(-scale)
            top = dec_limit(self.param) - step if self.param else decimal.Decimal(10 ** 7 + FRESH_SPAN)
            v = (top - k * step).quantize(step)
            if v <= 0:
                raise ValueError(f"DECIMAL{tuple(self.param)} key domain has no fresh value for code {code}")
        elif self.type == FLOAT:
            v = float(10 ** 7 + k)
        else:                   # BOOL keys: only two values exist
            v = bool(k % 2)
        assert v not in self.index, f"minted key value collides with pool value {v!r}"
        self.minted[v] = code
        return v

    def bounds(self):
        """(lo, hi) of codes Z3 may choose; fresh region is [fresh_lo, hi]."""
        if self.is_int:
            lits = [x for x in self.lits if x != NULLK]
            lo = min([1] + lits)
            fresh_lo = max([0] + [abs(x) for x in lits]) + 2
            return lo, fresh_lo, fresh_lo + FRESH_SPAN
        span = FRESH_SPAN
        if self.type == DEC and self.param:      # fresh values must fit DECIMAL(p, s): (0, 10^(p-s)) in steps of 10^-s
            span = min(span, int(dec_limit(self.param) * 10 ** (self.param[1] or 0)) - 2)
        return 0, OFFSET, OFFSET + span

    def fresh_rep(self):
        return self.bounds()[1]
