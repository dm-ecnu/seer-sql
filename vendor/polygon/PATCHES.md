# SEER deltas relative to Polygon

The vendored tree under `sql_testing/` stays byte-identical to upstream (commit f4a4c6d). SEER does not import it
at run time: Polygon's operator encodings were ported into typed, in-process z3py terms in `src/seer/encode.py`
(Polygon emits an untyped integer formula AST through its own SMT layer and parses SQL with mo-sql-parsing, which
fails on 105 of the 203 distinct TPC-H rewrite pairs in our workload). Each element below is marked
**kept** (same semantics as Polygon, re-expressed), **fixed** (Polygon's encoding is wrong or lossy for SQL) or
**new** (no counterpart in Polygon).

## Database and constraints (`formulas/scan.py`, `formulas/integrity_constraint.py`, `variables.py`)

| Element | Status | Polygon | SEER (`encode.Enc`) |
|---|---|---|---|
| k tuple slots per table with an existence bit (`Deleted`) | kept | `scan.py` | `vars[t]`, prefix symmetry breaking (slot i+1 exists only if slot i exists) is new |
| Cell = (value, is-null) pair | kept | `variables.py` | `(var, nul)`; NOT NULL columns get a constant-false null bit |
| Value sorts | fixed | every value is `Int`; strings hashed to ints, floats truncated | INT -> Int; DECIMAL(p,s) -> scaled Int with exact rational arithmetic; FLOAT -> Real; strings -> index into a finite pool `Domain` (sorted, so `<` on indices is string order); DATE -> day number; TIMESTAMP -> seconds |
| PRIMARY KEY / UNIQUE | fixed | single-column, NULLs not distinguished | composite keys; UNIQUE ignores rows with any NULL key column (SQL semantics) |
| FOREIGN KEY | fixed | single-column only | composite FK with MATCH SIMPLE (a child row with any NULL FK column is exempt) |
| CHECK | new | not encoded | 3VL: the row violates only when the predicate is FALSE |
| Column type ranges (VARCHAR length via the pool, DECIMAL precision) | new | — | pool filtered by `seer2.pools._strlen`; decimal range from (p,s) |

## Relational operators (`formulas/*.py`, `visitors/query_encoder.py`)

| Element | Status | Notes |
|---|---|---|
| Selection (existence AND predicate TRUE) | kept | 3VL predicate = (true-bit, null-bit) |
| Projection | kept | |
| Inner / cross join | kept | |
| LEFT JOIN padding | kept | |
| RIGHT JOIN | fixed | Polygon swaps the inputs and thereby the output column order |
| FULL JOIN | fixed | Polygon's encoding drops right-only padding rows |
| SEMI / ANTI join, LATERAL (inner/cross/left) | new | |
| GROUP BY via representative tuple + in-group flags | kept | `formulas/group_by.py`; keys compared with NULL-safe equality |
| HAVING without GROUP BY | fixed | Polygon rewrites it into WHERE; SEER keeps the single (possibly empty-input) group |
| Aggregates count/count(*)/sum/avg/min/max | kept/fixed | empty-input results (count 0, others NULL) fixed; NULL-skipping fixed; avg is exact rational; DISTINCT and FILTER are new |
| Detection of aggregate queries | fixed | Polygon matches the substring "agg" in names; SEER uses the parsed tree |
| DISTINCT (keep the first occurrence) | kept | `formulas/distinct.py` |
| UNION / UNION ALL | kept | `formulas/union.py` |
| INTERSECT / EXCEPT (set and ALL) | new | multiplicity arithmetic min(m1,m2) / max(m1-m2,0) |
| Window functions ROW_NUMBER, RANK, DENSE_RANK, aggregate OVER (default, whole-partition and ROWS-to-current frames) | new | ties under ROW_NUMBER / ROWS frames are a runtime-nondeterminism condition (see Errors) |
| ORDER BY | fixed | parsed but unused by Polygon; SEER gives each output row a position term and compares lists when both queries are ordered and tie-free |
| LIMIT / OFFSET | new | ties across the cut are a nondeterminism condition |
| Non-recursive CTEs | new | inlined; shared by structural hash |

## Expressions (`visitors/expression_encoder.py`, `visitors/predicate_encoder.py`)

| Element | Status | Notes |
|---|---|---|
| Comparisons, AND/OR/NOT, IS [NOT] NULL, arithmetic | kept | 3VL truth tables made explicit |
| Integer division, modulo, division by zero | fixed | per-dialect: DuckDB `/` on integers is FLOAT, PostgreSQL/SQLite truncate, MySQL is DECIMAL; x/0 is a runtime-error condition |
| IN / NOT IN (list and subquery) | fixed | Polygon returns null=False, so NOT IN with a NULL on either side is wrong |
| ANY / ALL | new | 3VL: ANY is TRUE iff some row compares TRUE, FALSE iff all compare FALSE (dually for ALL) |
| EXISTS | kept | |
| Scalar subquery | fixed | Polygon reads cell (0,0) unchecked; SEER returns NULL on 0 rows and records ">1 row" as a runtime-error condition |
| Correlated subqueries | kept/fixed | de Bruijn outer references; memoised per outer-row environment |
| CASE / COALESCE / NULLIF / IF | kept | |
| ROUND / EXTRACT / string concatenation | fixed | silently skipped by Polygon; SEER encodes ROUND (half away from zero), EXTRACT(year/month/day), and string maps (`||`, upper/lower/trim/substr/left/right/replace) as lookups on the finite pool with closure, LENGTH/INSTR/STRPOS as lookups |
| LIKE / ILIKE, constant regex | new | pool lookup; regex limited to the LIKE-expressible subset (anchors, literals, `.`, `.*`, one alternation group) |
| DATE arithmetic (± INTERVAL day/month/year with month-end clamping, DATE_TRUNC, date differences) | new | |
| Unsupported constructs | fixed | Polygon skips them silently; SEER raises a typed `Unsupported(code)` and the query is reported, never encoded partially |

## Errors and nondeterminism (new)

Runtime errors (division by zero, scalar subquery with more than one row, string results outside the pool) and
nondeterminism (ties under LIMIT, ROW_NUMBER and ROWS frames) are collected per query as conditions; the solver
must produce a database on which every query of the class is error-free and deterministic. On the engine side
seer2's tie-break variants (LIMIT) and a row-reversed re-execution (row-order-sensitive constructs) mark
nondeterministic outputs, which never refine a class.

## Search (replaces `test.py` / `visitors/underapproximator.py` disambiguation)

Polygon checks one pair at a time and, for disambiguation, asks for a database splitting the queries into
`num_groups = 2`. SEER replaces this with batch partition refinement (`src/seer/symbolic.py`, `core.py`): one
shared encoding per bound k (k = 1, 2, 4) for all still-unsplit queries; each round asks, inside push/pop, for a
database on which some class is non-uniform, greedily augments the model to separate as many still-together pairs
as possible, executes the decoded database on the real engine and refines the partition only by engine outputs.
A fast tier of random constraint-valid databases (SQLWitness-style value pools with repair) runs first. Polygon's
conflict-driven under-approximation is not used.

## Differential check against the unmodified Polygon (`scripts/seer_polygon_diff.py`)

Both tools get the constraints Polygon's input language expresses (PK, single-column FK, NOT NULL; CHECK, UNIQUE
and composite FKs dropped for both), bound 2 (Polygon `default_k=16` per operator, author setting), 120 s per pair;
SEER runs only its symbolic tier at k=2. Every disagreement is decided on DuckDB with an engine-checked witness.
Run 2026-10-21 on eval-server (`runs/server/polydiff/diff.jsonl`), 256 pairs (185 TPC-H rewrite pairs with a
non-error label, 71 coverage-suite cases):

- Polygon decides 88 pairs (TPC-H 48, cases 40); it returns ERR on 143 (parse/encode failures) and TIMEOUT on 25.
  SEER decides all 88; the verdicts agree on 78 (TPC-H 45/48, cases 33/40).
- All 10 disagreements are Polygon errors, none a SEER error:
  - Polygon EQ, SEER NEQ with an engine-reproduced Σ-valid witness of ≤2 rows/table (Polygon misses a
    counterexample inside its own bound): TPC-H 6.6, 6.9; cases having_no_groupby, row_number_partition,
    order_dir. For the three cases the construct is one listed as fixed above (HAVING without GROUP BY, ORDER BY,
    window functions); the cause in TPC-H 6.6/6.9 has not been traced.
  - Polygon NEQ, SEER EQ, and Polygon's counterexample does not separate the queries on the engine (spurious):
    TPC-H 18.8; cases row_number_vs_rank_det, row_number_tiebreak, dense_rank_vs_rank (the last is truly
    non-equivalent but needs 3 rows, beyond the bound), win_over_agg (counterexample holds values outside INT32,
    which the engine refuses to load: untyped Int cells).
