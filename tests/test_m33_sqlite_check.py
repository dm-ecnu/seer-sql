import sqlite3

import sqlglot

from seer2.schema import eval_check


def test_check_mixed_storage_classes_match_sqlite():
    db = sqlite3.connect(":memory:")
    values = [None, -2, 0, 1, 1.0, 1.5, 2**53 + 1, float(2**53), "", "1", "abc", b"", b"1"]
    for op in ("=", "!=", "<", "<=", ">", ">="):
        expr = sqlglot.parse_one(f"a {op} b", read="sqlite")
        db.execute(f"CREATE TABLE test (a, b, CHECK (a {op} b))")
        for a in values:
            for b in values:
                actual = eval_check(expr, {"a": a, "b": b})
                expected = db.execute(f"SELECT ? {op} ?", (a, b)).fetchone()[0]
                assert actual is (None if expected is None else bool(expected)), (a, op, b, actual, expected)
                try:
                    db.execute("INSERT INTO test VALUES (?, ?)", (a, b))
                    accepted = True
                except sqlite3.IntegrityError:
                    accepted = False
                assert accepted == (expected is None or bool(expected)), (a, op, b)
        db.execute("DROP TABLE test")
