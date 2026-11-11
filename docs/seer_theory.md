# SEER: problem, guarantees, and how they are checked

## 1. Setting

A schema Σ consists of typed tables with NOT NULL, PRIMARY KEY, UNIQUE, FOREIGN KEY (composite, MATCH SIMPLE)
and CHECK constraints. A database D is Σ-valid if it satisfies all of them; CHECK fails only when its predicate
evaluates to FALSE. The reference semantics of a query Q on D is the output of the engine, `out(Q, D)` (the dataset's own engine:
DuckDB, SQLite, PostgreSQL 16 or MySQL 8 with the evaluation's sql_mode). That output is
a bag of rows; it is also a list when the outermost ORDER BY is tie-free on D. Q is *well-defined* on D when:

- the engine raises no runtime error;
- the output does not depend on tie resolution, as checked by seer2's tie-break variants for LIMIT and by a
  row-reversed re-execution for ROW_NUMBER, ROWS frames, value windows and bare columns.

D *separates* Q and Q' when:

- both are well-defined on D, and
- their bags differ, or both are ordered and their lists differ.

Q ≡_Σ Q' means that no Σ-valid D separates them.

**Batch problem.** Given Q1..Qn, compute the partition of {Q1..Qn} into classes of queries that no database
separates, together with a set of witness databases (certificates) that justifies every split.

## 2. Hardness of a minimum certificate set

**MinDS**: given queries and a finite candidate space 𝒟 of databases, find a smallest W ⊆ 𝒟 such that every pair
separated by some database in 𝒟 is separated by some database in W.

**Proposition 1.** MinDS is NP-hard, even with one single-column table, k = 1 tuple, and queries in the
fragment SELECT count(*) … WHERE c IN (constant list).

*Proof.* The reduction is from Minimum Test Collection, which is NP-complete (Garey & Johnson, problem SP6). An
instance is a finite set S and a family C of subsets of S. The task is to find a smallest C' ⊆ C such that for
every two distinct s, s' ∈ S some c ∈ C' contains exactly one of them.

Build the following instance of MinDS:

- Schema: one table T(c INTEGER).
- One query per element s ∈ S: q_s = `SELECT count(*) FROM T WHERE c IN (ids of the tests containing s)`.
- Candidate space: all databases with at most one tuple whose value is a test id or one fixed non-id value, plus
  the empty database.

On D_c = {T: [c]}, q_s returns 1 if s ∈ c and 0 otherwise. The empty database and the non-id database return 0
for every query, so they separate nothing. Therefore D_c separates q_s and q_s' exactly when c distinguishes s and
s'. A smallest distinguishing database set is then exactly a smallest test collection. The construction is
polynomial in |S| + |C|. ∎

Without a bound, even deciding whether one pair can be separated is undecidable for first-order queries
(Trakhtenbrot). Within a bound k it is NP-complete: the bounded encoding is a polynomial-size SMT formula over
decidable theories (linear integer and real arithmetic, Booleans), and a separating database is a polynomial
certificate.

## 3. Greedy refinement and its approximation ratio

Let U be the set of pairs that some database in 𝒟 separates, with m = |U|. Each database D covers a fixed set
sep(D) ⊆ U. The cover is independent of the current partition, so MinDS is Set Cover over U. SEER's round rule
is: pick a database that separates the largest number of pairs that are still together.

This is the classical greedy rule, and with an exact maximization oracle it gives |W_greedy| ≤ H(m)·OPT ≤ (1 + ln m)·OPT
(Johnson 1974; Chvátal 1979).

**Oracle quality.** If each round's oracle returns a database covering at least a 1/α fraction of the best
possible new coverage, the bound becomes α·H(m).

SEER's oracle is weaker than exact maximization:

1. The symbolic tier asks the solver for a database that separates at least one still-together pair of some
   class.
2. It then improves the model `augment` times (3 by default). Each step keeps every pair the model already
   separates and asks for one more.

The result is a maximal improvement within a fixed number of steps, not a maximum. The fast tier picks the
best of its random candidates.

The (1 + ln m) bound therefore describes the rule SEER implements with an exact oracle. The implementation gives
no worst-case α. We report empirically:

- the certificate count per batch;
- pairs separated per certificate, from the stored events.

## 4. Soundness of splits

**Proposition 2.** If SEER puts Q and Q' in different classes, then Q ≢_Σ Q'.

*Argument.* A class is refined only in `Seer.run_db`, and only from engine outputs. The database D comes from
either tier and is checked as follows:

1. **Σ-validity.** D is checked with `Schema.violations`, which is a runtime assertion in `FastGen.next`, in
   the refine tier for every instance it hands over, and after decoding every symbolic model. The engine also
   loads D with the constraints declared.
2. **Engine execution.** All queries of the class run on the engine. `split_class` refines the class only when
   every output is well-defined (§1). Blocks whose rows agree up to float rounding (relative 1e-6) are merged.
3. **Re-check.** At the end of `run()`, every refinement event is re-executed on a fresh connection and must
   reproduce the same blocks (`Seer.recheck`). A replay that errs (a statement timeout on a loaded machine) or
   differs is retried up to three times. A certificate that never reproduces is withdrawn: it is not exported,
   and every pair that only it separated is reported `undecided` (`certificate_not_reproduced`), never `split`.
   The proposition is therefore about the exported certificates, and withdrawal costs recall only.
4. **Certificate store.** D is stored in the certificate store; `python -m seer replay` re-executes stored
   results independently. Rows of a self-referencing table are executed and stored parents first
   (`loadable_order`). Σ holds on D as a set of rows, but a loader that inserts row by row with immediate FK
   checks, such as the validator or an engine without deferred constraints, needs each referenced row first.
   A reference cycle (two rows that reference each other) has no such order. On an engine whose FK checks are
   immediate (MySQL), no load can hold such an instance, so it is never used as a certificate there
   (`cyclic_reference_skipped`).

The refine tier (the diff-targeted witness search of the seer2 prototype: predicate witnesses over the value
pools, aggregate witnesses for GROUP BY / HAVING / DISTINCT / NULL sensitivity, ranked mutations) runs first. Its
own splitting decisions are not trusted. Each instance that refined its partition is replayed through
`Seer.run_db`, so it goes through steps 1–4 like any other database, and the split is accepted only if the engine
reproduces it under SEER's rules. The replay happens the moment the refiner adopts an instance (a callback), so the
time of a split is the time it was found. The refiner stops once SEER's partition has only singletons. Its search is
randomised and ends when its targets saturate. A saturated run that leaves classes unsplit is restarted with a
fresh seed on the remaining classes while the tier's budget lasts (at most three runs), so a split that one random
run misses is not lost to sampling. The tier first gets a share of the budget that grows with the share of live queries outside the encoding: half
the budget when every query is encodable, the whole budget, in one continuous run, when none is. Whatever the symbolic tier leaves unused goes back to
the refine tier, which then works only on the classes that are neither split nor proved bounded-equivalent. These
are typically classes with constructs outside the encoding (date arithmetic, value windows), so the refine tier is
the only tier that can split them, and it gets the whole budget, as the v2 prototype does.

Well-definedness under LIMIT is checked with two tie-break variants that append the output columns to the ORDER BY:
one in ascending order with NULLS FIRST, one in descending order with NULLS LAST. The two are exact reverses,
NULLs included. If the NULL placement is left implicit, the SQL generator renders SQLite's ascending variant as
NULLS LAST, so both variants agree on a tie between a NULL row and a non-NULL row. The tie then goes unnoticed and
yields a split that a different tie resolution does not reproduce.

Soundness depends neither on the SMT encoding nor on the solver. A wrong encoding can only cost time: a model
whose predicted split the engine does not confirm is excluded with a blocking clause and counted as
`model_mismatch`. ∎

## 5. Relative completeness of `bounded_equivalent(k)`

The search space 𝒟_k(Dom) is the set of Σ-valid databases that satisfy:

- at most k tuples per table;
- strings drawn from the finite pool Dom;
- dates within the year range Dom.years (the literal years ± 1);
- integers, decimals and floats unbounded (Int / Real sorts; DECIMAL(p,s) bounded by its precision).

The pool contains:

- every string literal of the batch;
- strings matching and nearly matching each LIKE pattern;
- seeds ("", "a", "b", "zz") and one-character neighbours;
- the closure of the pool under the batch's string maps, two rounds, up to 1500 strings.

**Faithfulness assumption (F).** Take any encodable query Q (no `Unsupported` raised) and any D ∈ 𝒟_k(Dom).
Evaluating the encoding of Q under the assignment that represents D gives:

- the engine's output bag on D, and its list when the output is ordered and tie-free;
- error and nondeterminism conditions that are true exactly when Q is not well-defined on D.

**Proposition 3.** Under (F), assume SEER stops a class C at level k with UNSAT and every member of C is
encodable. Then no D ∈ 𝒟_k(Dom) separates any two members of C. This is what `bounded_equivalent` with that k
reports.

*Argument.* The round goal at level k is:

> ⋁_C [ valid(C) ∧ ⋁_{q ∈ C} sep(rep(C), q) ]

where:

- `valid(C)` says that no member of C has an error or nondeterminism condition;
- `sep` is the bag inequality by multiplicities, or, when both queries are ordered and tie-free, the list
  inequality.

Every slot has an existence bit, so every database with at most k tuples per table is an assignment. The goal is
asserted together with Σ inside push/pop. A separating D ∈ 𝒟_k(Dom) would satisfy the goal by (F), so UNSAT
means none exists.

The relation is transitive inside a class. If the representative is unseparable from every member, all pairs are
unseparable, because a separation of q and q' on a well-defined D would also separate one of them from rep(C).

**Monotonicity.** Existence bits make 𝒟_k ⊆ 𝒟_{k'} for k ≤ k', so equivalence at k' implies equivalence at k. ∎

**What the guarantee leaves out.**

- **Beyond the bound or pool.** Databases with more than k tuples per table, strings outside Dom, and dates
  outside the year range are not covered.
- **Unencodable queries.** A query outside the fragment, or one whose encoding exceeds the size guard
  (`encoding_too_large`), makes its class `undecided`, with that reason recorded.
- **Solver limits.** A solver timeout or `unknown` result also gives `undecided`.
- **Σ outside the encoding.** Σ also contains the constraints the DDL cannot declare (inclusion dependencies to
  non-key columns, FKs between columns of different declared types); they are generated and checked as hard
  constraints by every tier. The encoding represents a value by a per-type code (an integer, or an index into Dom),
  so an inclusion between columns of different types has no exact encoding; the symbolic tier then declines the
  schema (`sigma_unsupported`) and the batch is decided by the engine tiers alone, ending `undecided` rather than
  `bounded_equivalent`.
- **Status of (F).** (F) is an assumption about the implementation and is not proved. It is checked in three ways:
  - Brute force (§7) compares the encoding with the engine on every database of a small domain.
  - Every SAT model is executed, and disagreements are counted as `model_mismatch`.
  - The per-construct coverage suite contains equal and unequal pairs for each trap.

## 6. Scaling the encoding: join blocks with key lookup

**Problem.** A join of t tables with at most k tuples each has k^t candidate output rows. The direct encoding
gives each one a slot with an existence bit and a cell for every column. On TPC-H at k = 2, Q8 (8 tables), Q5
and Q7 (6), Q2 and Q9 (5–6) took 50–111 s per group to encode, or timed out. In batch 10, encoding took 94 of
the 120 s. At k = 4, 81 of 226 queries could not be encoded at all.

**Mechanism.** A *join block* is a maximal tree of non-lateral INNER/CROSS joins, together with the WHERE filter
directly above it (`joins.flatten`). Under bag semantics the block equals the product of its leaves filtered by
the conjunction of all ON and WHERE conjuncts, in any join order. The encoder chooses the order (`joins.plan`) and
applies each conjunct as soon as all of its columns are joined. A conjunct containing a subquery counts as
reading the whole block.

The key step is a *lookup* join. Take a leaf L that is a base table, or a filter of one, with a PRIMARY KEY or
UNIQUE key K. Suppose every column of K is bound by an equality conjunct `L.k = f(row so far)` whose other side
reads only leaves already joined. Then for every partial row at most one existing tuple of L satisfies the
binding conjuncts, because two would share the non-NULL key value, which Σ forbids. The step therefore emits one
slot per partial row instead of k. The slot's cells are an if-then-else selection over L's k tuples, and it
exists iff some tuple matches. A chain of FK→PK joins over t tables thus has k slots instead of k^t. The planner
tries each leaf as the start, takes lookups whenever one applies, and otherwise adds the smallest connected leaf.
It keeps the order with the fewest estimated slots, and the same estimate drives the size guard (`est_cost`).

**Exactness.** The lookup step is not an approximation. It is equal to the product-and-filter encoding on every
assignment that satisfies Σ:

- the key constraints are hard constraints of every level;
- under them, at most one disjunct of the selection is true, so the selected cells are those of the unique match;
- when nothing matches, the slot does not exist, which is exactly when the product rows for that partial row
  would all have been filtered out.

Proposition 3 and assumption (F) are therefore unchanged. `test_join_block_bruteforce` checks this against the
engine on every database of a small domain, for: join reordering, an equality that holds only because the
joined column is a key, a non-key join that duplicates rows, aggregation over a lookup chain, and a filter on a
lookup leaf.

**Alternatives considered.**

- *Smaller per-table bounds for large joins.* This changes 𝒟_k and weakens the guarantee.
- *Symmetry breaking between slots.* This shrinks the solver's search but not the encoding, which was the
  bottleneck.
- *Caps on the size of intermediate results.* These are unsound for the completeness direction.

The key lookup removes the k^t factor in the case that dominates real workloads (FK→PK joins) without changing
what `bounded_equivalent(k)` means. Non-key joins still pay k per step.

**Measured effect.** Each TPC-H group is encoded as one level with the system's per-query cap (10 s at budget 120)
(`scripts/seer_encode_bench.py`, eval-server, one core per group, same time window). Before is commit c8bed24,
after is abd3633.

| bound | total encode time over 22 groups | queries encoded (of 226) |
|---|---|---|
| k = 2 | 382 s → 8.2 s | 207 → 226 |
| k = 4 | 464 s → 97 s | 145 → 224 |

Before, the k = 4 total was low only because most large-join groups hit the per-query cap and were skipped. At
k = 2, the largest group now encodes in 1.3 s. Solving is untouched by this change. The remaining level time is
dominated by solver `unknown` results at k = 4, about 20 s for one call in roughly half of the groups.

## 7. Runtime assertions and tests

Runtime assertions, active in every run:

- Σ-validity of every fast-tier database and every decoded symbolic model;
- the engine accepts every Σ-valid instance (`EngineRejected` is an assertion failure);
- every exported refinement is reproduced on a fresh connection (`recheck`; a non-reproducing one is withdrawn,
  counted as `recheck_dropped`);
- certificates replay (`python -m seer replay`).

Tests in `tests/test_seer_sys.py`, run locally in under 30 s:

- `test_coverage_symbolic`: 71 constructs, each with an equal and an unequal case. The trap cases are:
  - NULL in IN, NOT IN, ANY and ALL;
  - empty groups and HAVING without GROUP BY;
  - duplicates under DISTINCT, UNION ALL, EXCEPT ALL and INTERSECT ALL;
  - ties under ORDER BY, LIMIT and ROW_NUMBER;
  - outer-join padding: LEFT, RIGHT, FULL and LATERAL;
  - composite FK under MATCH SIMPLE with partial NULLs;
  - UNIQUE with NULLs;
  - CHECK under 3VL;
  - DECIMAL;
  - month-end date clamping;
  - LIKE and regex.

  Each case runs with the symbolic tier alone at k ∈ {1, 2, 4}.
- `test_coverage_fast_then_symbolic_batch`: the same cases with both tiers.
- `test_bruteforce_agrees`: the queries run on every database with |t| ≤ 2 and |u| ≤ 1 over {NULL, 0, 1}, with
  u(a) a primary key, which is 196 databases. If any of them separates the pair, SEER at k = 2 must split it; if
  none does, SEER must report `bounded_equivalent`. This checks the completeness direction of (F) on that domain.
  Soundness is enforced by the runtime assertions above.
- `test_join_block_bruteforce`: five join-block pairs over an FK chain r→q→p, compared with the engine on every
  database with |p| ≤ 1, |q| ≤ 2 and |r| ≤ 2 over small values with NULLs. The pairs cover join reordering, a
  key-dependent equality, a non-key join, aggregation over a lookup chain, and a filter on a lookup leaf.
- `test_incremental_add_query`: places a new query by replaying the certificate store, then refines its class.
- `test_pair_api_and_certificate_replay`, `test_unsupported_is_typed`.
