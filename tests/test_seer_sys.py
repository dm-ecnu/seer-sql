"""SEER system tests (seconds, local): per-construct coverage, brute-force cross-check over a tiny domain,
incremental add_query, certificate replay, typed Unsupported."""
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from seer2.backends import make_backend          # noqa: E402
from seer2.refine import Prepared, execute, split_class  # noqa: E402
from seer2.schema import Schema                  # noqa: E402
from seer import Seer as _Seer, Unsupported, judge_pair   # noqa: E402
from seer.front import Front                     # noqa: E402
from seer_cases import CASES, DDL                # noqa: E402

S = Schema(DDL)


def Seer(*a, **kw):
    """Unit tests check verdicts at the base levels (k <= 4) with one closing cycle so the suite runs in seconds;
    tests of deepening and of the closing loop pass their own settings."""
    kw.setdefault("max_k", 4)
    kw.setdefault("close_idle", 1)
    return _Seer(*a, **kw)


def _verdict(r, a="a", b="b"):
    v = r["verdicts"][f"{a}|{b}"]["verdict"]
    return {"split": "neq", "bounded_equivalent": "eq"}.get(v, v)


@pytest.mark.parametrize("name,a,b,exp", CASES, ids=[c[0] for c in CASES])
def test_coverage_symbolic(name, a, b, exp):
    r = Seer(S, [("a", a), ("b", b)], fast=False, levels=(1, 2, 4), budget=30).run()
    assert not r["unsupported"], r["unsupported"]
    assert _verdict(r) == exp, r["verdicts"]


def test_coverage_fast_then_symbolic_batch():
    """All cases as one batch per expected-eq pair groups would merge unrelated queries; run pairwise, both tiers."""
    # bounded equivalence (symbolic tier) quantifies over instances on which both results are order-independent;
    # a split may also come from an order-dependent instance whose observable results differ (row_number over a
    # tie never repeats a number, rank does): such cases are neq once the fast tier is on
    order_dep_neq = {"row_number_vs_rank_det"}
    bad = []
    for name, a, b, exp in CASES:
        exp = "neq" if name in order_dep_neq else exp
        r = Seer(S, [("a", a), ("b", b)], fast=True, fast_dbs=60, levels=(1, 2, 4), budget=30, seed=1).run()
        if _verdict(r) != exp:
            bad.append((name, r["verdicts"]))
    assert not bad, bad


# ------------------------------------------------------------------ brute force over a tiny domain
TINY = Schema("""
CREATE TABLE t (a INTEGER, b INTEGER);
CREATE TABLE u (a INTEGER PRIMARY KEY, c INTEGER);
""")
TINY_PAIRS = [
    ("SELECT a FROM t WHERE a NOT IN (SELECT a FROM u)",
     "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.a = t.a)"),
    ("SELECT a FROM t WHERE a IN (SELECT a FROM u)",
     "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a = t.a)"),
    ("SELECT count(*) FROM t", "SELECT count(b) FROM t"),
    ("SELECT sum(b) FROM t", "SELECT coalesce(sum(b), 0) FROM t"),
    ("SELECT DISTINCT a FROM t", "SELECT a FROM t GROUP BY a"),
    ("SELECT DISTINCT a FROM t", "SELECT a FROM t"),
    ("SELECT t.a, u.c FROM t LEFT JOIN u ON t.a = u.a", "SELECT t.a, u.c FROM t JOIN u ON t.a = u.a"),
    ("SELECT t.a, (SELECT c FROM u WHERE u.a = t.a) FROM t", "SELECT t.a, u.c FROM t LEFT JOIN u ON t.a = u.a"),
    ("SELECT a, count(b) FROM t GROUP BY a", "SELECT a, count(*) FROM t WHERE b IS NOT NULL GROUP BY a"),
    ("SELECT a, count(b) FROM t GROUP BY a HAVING count(b) > 0",
     "SELECT a, count(*) FROM t WHERE b IS NOT NULL GROUP BY a"),
    ("SELECT a FROM t UNION SELECT a FROM u", "SELECT a FROM t UNION ALL SELECT a FROM u"),
    ("SELECT a FROM t EXCEPT SELECT a FROM u", "SELECT a FROM t WHERE a NOT IN (SELECT a FROM u)"),
    ("SELECT a FROM t WHERE b > ALL (SELECT c FROM u)", "SELECT a FROM t WHERE b > (SELECT max(c) FROM u)"),
    ("SELECT a FROM t WHERE b = 1 OR b <> 1", "SELECT a FROM t WHERE b IS NOT NULL"),
    ("SELECT max(a) FROM t", "SELECT a FROM t ORDER BY a DESC LIMIT 1"),
    ("SELECT a, b FROM t ORDER BY a, b", "SELECT a, b FROM t ORDER BY a NULLS FIRST, b"),
    ("SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b", "SELECT a, min(b) FROM t GROUP BY a ORDER BY a"),
    ("SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b NULLS FIRST", "SELECT a, min(b) FROM t GROUP BY a ORDER BY a"),
    ("SELECT x.a FROM (SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b) x WHERE x.b IS NULL",
     "SELECT a FROM t GROUP BY a HAVING count(b) = 0"),
    ("SELECT a, count(DISTINCT b) OVER (PARTITION BY a) FROM t", "SELECT a, count(b) OVER (PARTITION BY a) FROM t"),
    ("SELECT a, count(DISTINCT b) OVER (PARTITION BY a) FROM t",
     "SELECT t.a, s.c FROM t JOIN (SELECT a, count(DISTINCT b) AS c FROM t GROUP BY a) s ON t.a IS NOT DISTINCT FROM s.a"),
]


def _tiny_dbs():
    vals = [None, 0, 1]
    trows = [{"a": a, "b": b} for a in vals for b in [None, 1]]
    tbags = [[]] + [[r] for r in trows] + [[trows[i], trows[j]] for i in range(len(trows)) for j in range(i, len(trows))]
    ubags = [[]] + [[{"a": a, "c": c}] for a in [0, 1] for c in vals]
    for tb, ub in itertools.product(tbags, ubags):
        yield {"t": tb, "u": ub}


def _brute_differs(qa, qb):
    be = make_backend("duckdb", TINY, 10)
    pa, pb = Prepared(qa, "duckdb", be.dialect), Prepared(qb, "duckdb", be.dialect)
    for db in _tiny_dbs():
        inst = be.open(db)
        try:
            outs = {"a": execute(inst, pa), "b": execute(inst, pb)}
        finally:
            inst.close()
        if len(split_class(["a", "b"], outs)) > 1:
            return db
    return None


@pytest.mark.parametrize("qa,qb", TINY_PAIRS)
def test_bruteforce_agrees(qa, qb):
    """Brute-force difference within (t<=2, u<=1) over {NULL,0,1} implies SEER splits at k=2 (relative
    completeness); SEER's split is always engine-confirmed (soundness, re-checked inside run())."""
    r = Seer(TINY, [("a", qa), ("b", qb)], fast=False, levels=(1, 2), budget=30).run()
    w = _brute_differs(qa, qb)
    got = _verdict(r)
    if w is not None:
        assert got == "neq", (w, r["verdicts"])
    else:
        assert got == "eq", r["verdicts"]


# ------------------------------------------------------------------ join blocks with key lookup (joins.py)
CHAIN = Schema("""
CREATE TABLE p (a INTEGER PRIMARY KEY, x INTEGER);
CREATE TABLE q (b INTEGER PRIMARY KEY, a INTEGER REFERENCES p(a), y INTEGER);
CREATE TABLE r (c INTEGER, b INTEGER);
""")
CHAIN_PAIRS = [
    # same block, comma joins vs explicit joins in another order
    ("SELECT r.c, p.x FROM r, q, p WHERE r.b = q.b AND q.a = p.a",
     "SELECT r.c, p.x FROM p JOIN q ON q.a = p.a JOIN r ON r.b = q.b"),
    # equal only because q.b is a key (lookup must not lose or invent duplicates)
    ("SELECT r.c FROM r, q WHERE r.b = q.b", "SELECT r.c FROM r WHERE r.b IN (SELECT b FROM q)"),
    # q.a is not a key: the join duplicates r rows
    ("SELECT r.c FROM r, q WHERE r.b = q.a", "SELECT r.c FROM r WHERE r.b IN (SELECT a FROM q)"),
    ("SELECT p.x, count(*) FROM r JOIN q ON r.b = q.b JOIN p ON p.a = q.a GROUP BY p.x",
     "SELECT p.x, count(r.c) FROM r JOIN q ON r.b = q.b JOIN p ON p.a = q.a GROUP BY p.x"),
    ("SELECT r.c FROM r, q, p WHERE r.b = q.b AND q.a = p.a AND p.x = 1",
     "SELECT r.c FROM r, q WHERE r.b = q.b AND q.a IS NOT NULL"),
]


def _chain_dbs():
    prow = [{"a": a, "x": x} for a in (0, 1) for x in (None, 1)]
    qrow = [{"b": b, "a": a, "y": 0} for b in (0, 1) for a in (None, 0, 1)]
    rrow = [{"c": c, "b": b} for c in (None, 1) for b in (None, 0, 1)]

    def bags(rows):
        return [[]] + [[x] for x in rows] + [[rows[i], rows[j]] for i in range(len(rows)) for j in range(i, len(rows))]
    for pb in [[]] + [[x] for x in prow]:
        for qb in bags(qrow):
            db0 = {"p": pb, "q": qb, "r": []}
            if CHAIN.violations(db0):
                continue
            for rb in bags(rrow):
                yield {"p": pb, "q": qb, "r": rb}


@pytest.mark.parametrize("qa,qb", CHAIN_PAIRS)
def test_join_block_bruteforce(qa, qb):
    """Key-lookup join encoding is exact: a brute-force difference within (p<=1, q<=2, r<=2) implies a split at
    k=2, and no difference implies bounded equivalence at k=2."""
    be = make_backend("duckdb", CHAIN, 10)
    pa, pb = Prepared(qa, "duckdb", be.dialect), Prepared(qb, "duckdb", be.dialect)
    w = None
    for db in _chain_dbs():
        inst = be.open(db)
        try:
            outs = {"a": execute(inst, pa), "b": execute(inst, pb)}
        finally:
            inst.close()
        if len(split_class(["a", "b"], outs)) > 1:
            w = db
            break
    r = Seer(CHAIN, [("a", qa), ("b", qb)], fast=False, levels=(1, 2), budget=30).run()
    got = _verdict(r)
    if w is not None:
        assert got == "neq", (w, r["verdicts"])
    else:
        assert got == "eq", r["verdicts"]


# ------------------------------------------------------------------ API
def test_incremental_add_query():
    qs = [("q0", "SELECT eid FROM emp WHERE sal > 10"), ("q1", "SELECT eid FROM emp WHERE sal + 0 > 10"),
          ("q2", "SELECT eid FROM emp")]
    sr = Seer(S, qs, fast=False, levels=(1, 2), budget=30)
    sr.run()
    r = sr.add_query("q3", "SELECT eid FROM emp WHERE NOT sal <= 10")
    part = sorted(sorted(c) for c in r["classes"])
    assert part == [["q0", "q1", "q3"], ["q2"]], part
    r = sr.add_query("q4", "SELECT eid FROM emp WHERE sal >= 10")
    assert ["q4"] in [sorted(c) for c in r["classes"]]


def test_pair_api_and_certificate_replay():
    r = judge_pair(S, "SELECT did FROM dept WHERE did NOT IN (SELECT did FROM emp)",
                   "SELECT did FROM dept WHERE NOT EXISTS (SELECT 1 FROM emp WHERE emp.did = dept.did)",
                   fast=False, levels=(1, 2))
    v = r["pair"]
    assert v["verdict"] == "split"
    db = r["certificates"]["dbs"][v["cert"]]
    be = make_backend("duckdb", S, 10)
    from seer.core import from_jsonable
    inst = be.open(from_jsonable(db))
    try:
        oa = execute(inst, Prepared(r["queries"]["q1"], "duckdb", be.dialect))
        ob = execute(inst, Prepared(r["queries"]["q2"], "duckdb", be.dialect))
    finally:
        inst.close()
    assert oa.bag != ob.bag


def test_unsupported_is_typed():
    f = Front(S, "duckdb")
    with pytest.raises(Unsupported):
        f.translate("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r")
    r = Seer(S, [("a", "SELECT eid FROM emp"), ("b", "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r "
                                                       "WHERE n < 3) SELECT n FROM r")], fast=False).run()
    assert "b" in r["unsupported"]


# ---------------------------------------------------------------- robustness classes found on the SQLWitness datasets
SELFREF = Schema("""CREATE TABLE post (id INT PRIMARY KEY, parent INT REFERENCES post(id), kind VARCHAR(8),
                    "cross" INT, CHECK (kind IN ('view', 'click')));""", "sqlite")


@pytest.mark.parametrize("backend", ["sqlite", "duckdb"])
def test_load_self_reference_any_row_order_and_keyword_column(backend):
    """Σ is checked on the final instance: a row may reference one inserted after it; a column named like a keyword
    loads and is queryable."""
    db = {"post": [{"id": 1, "parent": 2, "kind": "view", "cross": 0}, {"id": 2, "parent": None, "kind": "click",
                                                                           "cross": 1}]}
    assert not SELFREF.violations(db)
    inst = make_backend(backend, SELFREF, 10).open(db)
    try:
        rows, _ = inst.run('SELECT count(*) FROM post WHERE "cross" = 1')
        assert rows == [(1,)]
    finally:
        inst.close()


def test_check_strings_outside_queries_are_encodable():
    """A CHECK constraint whose string literals no query mentions must not make Σ (and every level) unencodable."""
    r = Seer(SELFREF, [("a", "SELECT id FROM post WHERE parent IS NULL"), ("b", "SELECT id FROM post")],
             dialect="sqlite", backend="sqlite", budget=30, fast=False).run()
    assert r["stats"].get("sigma_unsupported") is None
    assert r["verdicts"]["a|b"]["verdict"] == "split"


def test_atom_eval_distinguishes_equal_values_of_different_types():
    from seer2.generate import AtomEval
    ae = AtomEval("duckdb", "duckdb")
    assert ae.unary("x0 > 1", [2, 2.0, 1], "float") == ["T", "T", "F"]


def test_undeclared_inclusion_is_part_of_sigma():
    """An FK to a non-key column cannot be declared in SQL; it is still part of Σ: generated, checked, encoded."""
    S2 = Schema("CREATE TABLE a (id INT PRIMARY KEY, st VARCHAR(4)); CREATE TABLE v (id INT PRIMARY KEY, st VARCHAR(4), "
                "c INT);", "sqlite", inclusions=[("v", ["st"], "a", ["st"])])
    assert "REFERENCES" not in " ".join(S2.ddl_for("sqlite"))
    assert S2.violations({"a": [], "v": [{"id": 1, "st": "x", "c": 0}]})
    r = Seer(S2, [("a", "SELECT count(*) FROM v WHERE st NOT IN (SELECT st FROM a)"),
                  ("b", "SELECT count(*) FROM v WHERE st IS NOT NULL")], dialect="sqlite", backend="sqlite",
             budget=30).run()
    v = r["verdicts"]["a|b"]
    assert v["verdict"] == "split"
    from seer.core import from_jsonable
    assert not S2.violations(from_jsonable(r["certificates"]["dbs"][v["cert"]]))


def test_mixed_type_key_domain_keeps_only_representable_values():
    """A text column joined to an integer key (SQLite FK across types): its literal 'a' cannot match any key value;
    it is dropped from that column's candidates instead of crashing the integer domain."""
    S2 = Schema("CREATE TABLE p (id INT PRIMARY KEY); CREATE TABLE c (k INT PRIMARY KEY, pid TEXT NOT NULL);", "sqlite",
                inclusions=[("c", ["pid"], "p", ["id"])])
    r = Seer(S2, [("a", "SELECT count(*) FROM c WHERE pid = 'a' OR k > 1"),
                  ("b", "SELECT count(*) FROM c WHERE k > 1")], dialect="sqlite", backend="sqlite", budget=20).run()
    assert r["verdicts"]["a|b"]["verdict"] in ("bounded_equivalent", "undecided", "split")


def test_deep_clone_copies_each_dependant_once():
    """A dependant referencing the cloned row through several FKs (a match with many player columns) is copied once,
    with every such FK redirected; cloning FK by FK used to re-copy the copies (2^#FK rows)."""
    import random
    from seer2.generate import Mutator
    from seer2.pools import Pools
    fks = ", ".join(f"p{i} INT REFERENCES p(id)" for i in range(12))
    S2 = Schema(f"CREATE TABLE p (id INT PRIMARY KEY); CREATE TABLE m (id INT PRIMARY KEY, {fks});")
    db = {"p": [{"id": 1}], "m": [{"id": 1, **{f"p{i}": 1 for i in range(12)}}]}
    mut = Mutator(S2, Pools(S2, ["SELECT * FROM m"]), random.Random(0))
    assert mut.clone(db, "p", db["p"][0], deep=True)
    assert len(db["p"]) == 2 and len(db["m"]) == 2
    new = db["p"][1]["id"]
    assert all(db["m"][1][f"p{i}"] == new for i in range(12)) and not S2.violations(db)


@pytest.mark.parametrize("backend", ["sqlite", "duckdb"])
def test_limit_tie_between_null_and_value_is_nondeterministic(backend):
    """ORDER BY k LIMIT 1 with a tie on k between a row whose output is NULL and one whose output is a value."""
    from seer2.backends import make_backend
    from seer2.refine import Prepared, execute
    S2 = Schema("CREATE TABLE t (id INT PRIMARY KEY, k TEXT, m TEXT);", backend)
    inst = make_backend(backend, S2).open({"t": [{"id": 1, "k": "0", "m": None}, {"id": 2, "k": "a", "m": "x"}]})
    q = "SELECT m FROM t ORDER BY CAST(k AS REAL) LIMIT 1" if backend == "sqlite" else \
        "SELECT m FROM t ORDER BY length(k) LIMIT 1"
    assert execute(inst, Prepared(q, backend, backend)).kind == "nondet"


def test_saturated_refiner_is_restarted_with_fresh_seeds():
    """Equal queries: every refiner run saturates without a split; the tier restarts it (bounded rounds) and the
    pair still ends bounded_equivalent."""
    S2 = Schema("CREATE TABLE t (id INT PRIMARY KEY, a INT);")
    r = Seer(S2, [("a", "SELECT id FROM t WHERE a > 1"), ("b", "SELECT id FROM t WHERE 1 < a")], budget=30).run()
    assert r["verdicts"]["a|b"]["verdict"] == "bounded_equivalent"
    assert 1 <= r["stats"].get("refine_rounds", 0) <= 3


def test_date_domain_stays_inside_the_calendar():
    """A literal on the calendar's edge (9999-12-31) used to push the encoded year range to 10000."""
    r = Seer(Schema("CREATE TABLE t (id INT PRIMARY KEY, d DATE);"),
             [("a", "SELECT id FROM t WHERE d <= DATE '9999-12-31'"), ("b", "SELECT id FROM t WHERE d IS NOT NULL")],
             fast=False, levels=(1,), budget=20).run()
    assert r["verdicts"]["a|b"]["verdict"] in ("bounded_equivalent", "split", "undecided")


def test_boolean_value_against_integer_enum():
    from seer2.schema import _cmp
    assert _cmp(True, 1, "eq") is True and _cmp(False, 1, "eq") is False


def test_self_reference_rows_are_emitted_parents_first():
    """Σ holds on the set of rows; a row-by-row loader with immediate FK checks needs the manager first."""
    from seer.core import loadable_order
    S2 = Schema("CREATE TABLE e (id INT PRIMARY KEY, boss INT REFERENCES e(id));")
    db = {"e": [{"id": 5, "boss": 6}, {"id": 4, "boss": 5}, {"id": 6, "boss": None}, {"id": 7, "boss": 7}]}
    ids = [r["id"] for r in loadable_order(S2, db)["e"]]
    assert ids.index(6) < ids.index(5) < ids.index(4) and sorted(ids) == [4, 5, 6, 7]


def test_unreproducible_certificate_is_withdrawn_not_fatal():
    """A certificate whose replay never reproduces (e.g. a statement timeout on a loaded machine) is withdrawn:
    its pairs become undecided, the batch completes, and the exported certificates replay exactly."""
    from seer.core import replay
    sr = Seer(S, [("a", "SELECT 1"), ("b", "SELECT 2")], fast=True, budget=10)
    sr.recheck_tries = 1
    real = sr._exec_all
    sr._exec_all = lambda inst, db, qs: {q: real(inst, db, [q])[q] if q == "a" else
                                         __import__("seer2.refine", fromlist=["Out"]).Out("err", msg="timeout")
                                         for q in qs} if sr.events else real(inst, db, qs)
    r = sr.run()
    assert r["verdicts"]["a|b"] == {"verdict": "undecided", "reason": "certificate_not_reproduced"}
    assert r["certificates"]["events"] == []
    assert replay(S, r) == 0


def test_percent_sign_in_identifier_loads():
    """BIRD column names such as "Percent (%) Eligible" in a parametrised INSERT (PostgreSQL / MySQL drivers)."""
    import os
    try:
        be = make_backend("postgres", Schema('CREATE TABLE t (id INT PRIMARY KEY, "Pct (%) x" INT);', "postgres"))
    except Exception:
        pytest.skip("no PostgreSQL here")
    col = be.schema.tables["t"].colnames[1]
    inst = be.open({"t": [{"id": 1, col: 3}]})
    inst.close()


def test_reference_cycle_is_reported():
    from seer.core import loadable_order
    S2 = Schema("CREATE TABLE e (id INT PRIMARY KEY, boss INT REFERENCES e(id));")
    _, ok = loadable_order(S2, {"e": [{"id": 0, "boss": 1}, {"id": 1, "boss": 0}]}, report=True)
    assert not ok
    _, ok = loadable_order(S2, {"e": [{"id": 0, "boss": 1}, {"id": 1, "boss": None}]}, report=True)
    assert ok


# ---------------------------------------------------------------- DECIMAL(p, s) bounds (TPC-DS numeric(5,2) keys)
NARROW = Schema("""
CREATE TABLE band (lo DECIMAL(5,2) PRIMARY KEY, name VARCHAR(10));
CREATE TABLE item (iid INT PRIMARY KEY, price DECIMAL(5,2), lo DECIMAL(5,2) REFERENCES band(lo));
""")


def test_decimal_values_respect_precision():
    from seer2.pools import Pools, coerce
    assert coerce(1000, "dec", (5, 2)) is None and coerce("999.99", "dec", (5, 2)) is not None
    sqls = ["SELECT iid FROM item WHERE price > 5000", "SELECT iid FROM item WHERE price >= 12.5"]
    p = Pools(NARROW, sqls)
    assert all(v is None or abs(v) < 1000 for v in p.col[("item", "price")])
    d = p.domain_of("band", "lo")
    lo, fresh, hi = d.bounds()
    assert all(abs(d.decode(c)) < 1000 for c in (fresh, fresh + 7, hi))


@pytest.mark.parametrize("fast", [True, False])
def test_decimal_keys_load_and_split(fast):
    a = "SELECT i.iid FROM item i JOIN band b ON i.lo = b.lo WHERE i.price > 12.5"
    b = "SELECT i.iid FROM item i JOIN band b ON i.lo = b.lo WHERE i.price >= 12.5"
    r = Seer(NARROW, [("a", a), ("b", b)], fast=fast, fast_dbs=60, levels=(1, 2), budget=30, seed=3).run()
    assert not any(k.startswith("engine_rejected") for k in r.get("stats", {})), r.get("stats")
    assert _verdict(r) == "neq", r["verdicts"]


# ---------------------------------------------------------------- mechanism classes from the first matrix wave
AIR = Schema("""
CREATE TABLE airlines (uid INT PRIMARY KEY, airline VARCHAR(20), abbr VARCHAR(5), country VARCHAR(10));
CREATE TABLE flights (fno INT PRIMARY KEY, airline INT REFERENCES airlines(uid), d TEXT, cost INT);
""")


def test_count_threshold_needs_large_witness():
    """HAVING COUNT(*) > 10 vs >= 10: the witness needs a group of exactly 10 rows (beyond the base levels)."""
    a = "SELECT a.airline FROM airlines a JOIN flights f ON a.uid = f.airline GROUP BY a.airline HAVING COUNT(*) > 10"
    b = "SELECT a.airline FROM airlines a JOIN flights f ON a.uid = f.airline GROUP BY a.airline HAVING COUNT(*) >= 10"
    sr = Seer(AIR, [("a", a), ("b", b)], fast=False, budget=60, seed=1, max_k=16)
    assert 10 in sr.level_schedule()
    assert _verdict(sr.run()) == "neq"


def test_split_by_order_invariant_row_count():
    from seer.core import split_class_n
    from seer2.refine import Out
    nd5 = Out("nondet", bag=("#n", 1), rows=[(5,)], msg="order")       # load-order result (5,)
    nd7 = Out("nondet", bag=("#n", 1), rows=[(7,)], msg="order")
    ok0 = Out("ok", bag=1, rows=[])
    ok5 = Out("ok", bag=2, rows=[(5,)])
    blocks = lambda c, o: sorted(map(sorted, split_class_n(c, o)))
    assert blocks(["a", "b"], {"a": nd5, "b": ok0}) == [["a"], ["b"]]       # several possible results vs one
    assert blocks(["a", "c"], {"a": nd5, "c": ok5}) == [["a", "c"]]         # replay would see the same rows
    assert blocks(["a", "d"], {"a": nd5, "d": nd7}) == [["a", "d"]]         # two order-dependent, same count
    assert blocks(["a", "b"], {"a": Out("nondet", msg="x"), "b": ok0}) == [["a", "b"]]   # nothing kept


def test_bare_column_row_count_split():
    """A bare (any_value) column makes rows order-dependent, but not the row count: 1 row vs none is a split."""
    a = "SELECT fno FROM flights GROUP BY airline HAVING COUNT(*) > 2"
    b = "SELECT fno FROM flights GROUP BY fno HAVING COUNT(*) > 2"
    r = Seer(AIR, [("a", a), ("b", b)], dialect="sqlite", backend="sqlite", fast=False, budget=60, seed=1).run()
    assert _verdict(r) == "neq", r["verdicts"]


def test_literal_through_date_functions():
    """STRFTIME('%Y', d) = '1997' and SUBSTR(d, 1, 10) put matching dates (and dates with a time) into d's pool."""
    a = "SELECT fno FROM flights WHERE date(SUBSTR(d, 1, 10)) BETWEEN '2019-09-10' AND '2019-11-19'"
    b = "SELECT fno FROM flights WHERE d BETWEEN '2019-09-10' AND '2019-11-19'"
    r = Seer(AIR, [("a", a), ("b", b)], dialect="sqlite", backend="sqlite", budget=60, seed=1).run()
    assert _verdict(r) == "neq", r["verdicts"]
    a = "SELECT fno FROM flights WHERE STRFTIME('%Y', d) = '1997'"
    b = "SELECT fno, cost FROM flights WHERE STRFTIME('%Y', d) = '1997'"
    r = Seer(AIR, [("a", a), ("b", b)], dialect="sqlite", backend="sqlite", budget=60, seed=1).run()
    assert _verdict(r) == "neq", r["verdicts"]


# ---------------------------------------------------------------- constraint-only domains (XData-BM university)
# a column whose CHECK admits no query constant and no type seed: its values come from the constants of Σ
CHECKED = Schema("""
CREATE TABLE course (course_id VARCHAR(8) PRIMARY KEY, credits NUMERIC(2,0) CHECK (credits > 0));
CREATE TABLE section (course_id VARCHAR(8) REFERENCES course(course_id), sec_id VARCHAR(8),
  semester VARCHAR(6) NOT NULL CHECK (semester IN ('Fall', 'Winter', 'Spring', 'Summer')),
  year NUMERIC(4,0) NOT NULL CHECK (year > 1701 AND year < 2100),
  PRIMARY KEY (course_id, sec_id, semester, year));
""")


def test_constraint_only_domains_have_values():
    from seer2.pools import Pools
    from seer2.schema import eval_check
    p = Pools(CHECKED, ["SELECT course_id FROM section WHERE sec_id = '1'"])
    for col in ("semester", "year"):
        vals = [v for v in p.col[("section", col)] if v is not None]
        assert vals, col
        for ch in CHECKED.tables["section"].checks:
            if ch.cols == [col]:
                assert all(eval_check(ch.expr, {col: v}) is not False for v in vals), (col, vals)


@pytest.mark.parametrize("fast", [True, False])
def test_constraint_only_domains_split_and_eq(fast):
    a = "SELECT s.course_id FROM section s JOIN course c ON s.course_id = c.course_id WHERE c.credits > 3"
    b = "SELECT s.course_id FROM section s JOIN course c ON s.course_id = c.course_id WHERE c.credits >= 3"
    e = "SELECT s.course_id FROM course c JOIN section s ON c.course_id = s.course_id WHERE 3 < c.credits"
    r = Seer(CHECKED, [("a", a), ("b", b), ("e", e)], fast=fast, fast_dbs=60, levels=(1, 2), budget=30, seed=3).run()
    assert _verdict(r, "a", "b") == "neq", r["verdicts"]
    assert _verdict(r, "a", "e") == "eq", r["verdicts"]
