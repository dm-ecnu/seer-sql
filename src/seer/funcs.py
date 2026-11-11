"""Python reference semantics of the scalar functions that the encoder evaluates by table lookup over the finite
string pool (and that the pool is closed under). One definition is used both for closing the pool and for building
the lookup tables, so the two cannot drift apart.
"""
from __future__ import annotations

import datetime as dt
import re

EPOCH = dt.date(1970, 1, 1)


def day_of(d: dt.date) -> int:
    return (d - EPOCH).days


def date_of(n: int) -> dt.date:
    return EPOCH + dt.timedelta(days=int(n))


def add_months(d: dt.date, m: int) -> dt.date:
    y, mo = divmod(d.month - 1 + m, 12)
    y += d.year
    mo += 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30,
            31][mo - 1]
    return dt.date(y, mo, min(d.day, last))


def _substr(s, start, length=None):
    # SQL SUBSTRING(s FROM start FOR length), 1-based; start <= 0 shortens the window (PostgreSQL/DuckDB rule)
    if length is not None and length < 0:
        return None
    if length is None:
        lo = max(start, 1)
        return s[lo - 1:]
    end = start + length       # exclusive, 1-based
    lo = max(start, 1)
    if end <= lo:
        return ""
    return s[lo - 1:end - 1]


STR_MAPS = {
    "upper": lambda s: s.upper(),
    "lower": lambda s: s.lower(),
    "trim": lambda s: s.strip(" "),
    "ltrim": lambda s: s.lstrip(" "),
    "rtrim": lambda s: s.rstrip(" "),
    "substr": lambda s, a, b=None: _substr(s, a, b),
    "left": lambda s, n: s[:max(n, 0)],
    "right": lambda s, n: s[len(s) - n:] if 0 < n <= len(s) else (s if n > len(s) else ""),
    "concat_r": lambda s, c: s + c,        # s || 'const'
    "concat_l": lambda s, c: c + s,        # 'const' || s
    "replace": lambda s, a, b: s.replace(a, b) if a else s,
}

STR_NUMS = {
    "length": lambda s: len(s),
    "instr": lambda s, sub: s.find(sub) + 1,
    "strpos": lambda s, sub: s.find(sub) + 1,
}


def like_regex(pattern: str, escape: str | None = None, case_insensitive=False):
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if escape and ch == escape and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out), re.S | (re.I if case_insensitive else 0))


def like(s, pattern, escape=None, ci=False):
    return like_regex(pattern, escape, ci).fullmatch(s) is not None


def like_examples(pattern):
    """A few strings matching / almost matching a LIKE pattern (pool seeds)."""
    core = pattern.replace("%", "").replace("_", "x")
    outs = {pattern.replace("%", "").replace("_", "x"), pattern.replace("%", "a").replace("_", "y"),
            pattern.replace("%", "").replace("_", "x") + "z", "z" + core}
    if core:
        outs.add(core[:-1])
    return outs
