# SEER

SEER is a partition-refinement algorithm for batch SQL equivalence checking.
Given a batch of candidate SQL queries over one schema with integrity
constraints, SEER indexes the queries by their results on a shared pool of
legal databases, splits classes whenever a new counterexample database
distinguishes their members, and verifies the remaining members of each class
against one representative with a shared schema and representative encoding.
It returns the partition, the separating databases and the verification label
(refuted, proved, bounded-equivalent or undecided) of every member.

SEER calls the official SQLWitness refuter and the SQLSolver prover; its
contribution is how a batch decides which comparisons reach these engines and
how one counterexample database is reused across the whole batch.

This repository is the code supplement of the ICDE 2027 submission
*SEER: Partition Refinement for Batch SQL Equivalence Checking*; the paper's
main comparison is reproduced as described in
[Reproducing the main comparison](#reproducing-the-main-comparison). Proofs and the
full experimental protocol are in the supplementary material (submitted as a
separate PDF).

---

## Snapshot

The tree is the frozen build used for the paper's main comparison
(LiveSQLBench, BIRD pool, LeetCode, XData-BM panels and the XData-BM ablation),
exported without development history:

| | |
|---|---|
| Source commit | `6a7e8eebfee3b9416a851c9b35aa505828031941` (tag `c69-objective-6a7e8eebfee3`) |
| Main-table driver | `experiments/c69/` (controller SHA-256 `c5022ddd…fac`, recorded in `experiments/c69/production-config.json`) |

Machine-specific absolute paths were replaced by environment variables (see
[Setup](#setup)); nothing else in the method code was changed.

---

## Repository layout

```
src/seer/            SEER algorithm
  stream.py            arrival, signature index over the database pool, class splits
  class_search.py      class-level block verification (one solver context per class)
  representative_search.py  member-vs-representative refutation
  encode.py            bounded SQL encoding (typed z3 terms, ported from Polygon)
  prover.py, stream_prover.py   bounded / full verification labels
  official_batch.py    calls into the official SQLWitness / SQLSolver engines
src/seer2/           schema, constraint-valid database generation, value pools
eval/seereval/       common batch format, DBMS backends (SQLite in-process, PostgreSQL)
eval/tools/          per-tool workers for the compared systems (official code, one pair per call)
eval/build/          converters from the public artifacts to the common batch format
scripts/             batch runners (run_stream_batch*.py), dataset preparation, matrix schedulers,
                     per-experiment check scripts
scripts/java/        SQLSolver adapter
experiments/c69/     driver, job list and summarizers of the frozen main comparison
tests/               unit and regression tests (pytest)
vendor/polygon/      Polygon (PLDI'25) source, unmodified, CC-BY-4.0 (see UPSTREAM.md, PATCHES.md)
vendor/xdatan/       install and run scripts for the official XDataN
vendor/sqldriller/   install scripts for SQLSolver / SQLDriller components
tools/sqlancer_observer/  SQLancer-based query-family generation on SQLite
data/                TPC-H SF0.01 schema, queries and data of the early TPC-H prototype
examples/            running example of the paper
```

---

## Install

Tested with Python 3.10.12 on Ubuntu 22.04 (x86-64), CPU only.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -q tests                     # some tests are skipped without the official tools
python examples/seer2_running_example.py
```

Key versions: `z3-solver 4.13.4`, `sqlglot 30.21.0`, SQLite 3.37.2 (Python's
`sqlite3`), `duckdb 1.5.6`. PostgreSQL 16 is needed only for XData-BM, where
XDataN's driver runs on PostgreSQL. SQLSolver needs JDK 17; XDataN needs JDK 11.

---

## Setup

Scripts read the workspace from environment variables instead of fixed paths:

| Variable | Meaning |
|---|---|
| `SEER_ROOT` | workspace root holding `datasets/`, `repro/` (run outputs) and the official tools |
| `TMPDIR` | scratch directory (tmpfs recommended for XDataN) |
| `XD_ROOT`, `SD_ROOT`, `SEER_TOOLS_ROOT` | install roots of XDataN, SQLSolver/SQLDriller and the other official tools (default under `SEER_ROOT`) |
| `LLM_API_KEY`, `LLM_URL` | OpenAI-compatible endpoint, only for regenerating LLM rewrites (`eval/build/gen_rewrites.py`) |

The batch drivers in `experiments/c69/` and `scripts/` expect this checkout at
`$SEER_ROOT/seer-c69-objective-code` (`ln -s "$PWD" "$SEER_ROOT/seer-c69-objective-code"`).

---

## Data

All workloads are built from public artifacts; none are redistributed here.

| Workload | Source |
|---|---|
| Spider, BIRD, LeetCode, Calcite, Literature pairs | SQLWitness artifact, Zenodo [10.5281/zenodo.23396996](https://doi.org/10.5281/zenodo.23396996) (LeetCode/Calcite/Literature originate from VeriEQL) |
| BIRD pool, LiveSQLBench | public candidate pools prepared by `scripts/prepare_public_batches.py` from the released BIRD and LiveSQLBench (base-lite, SQLite) data |
| XData-BM | XDataN repository, [gitlab.com/xdata/xdatapublic](https://gitlab.com/xdata/xdatapublic) @ `a860311` |
| Cubes, TPC-H rewrites | Polygon artifact, Zenodo [10.5281/zenodo.15048096](https://doi.org/10.5281/zenodo.15048096) |

```bash
python eval/build/build_datasets.py --raw "$SEER_ROOT/datasets/raw" --out "$SEER_ROOT/datasets/batches"
python scripts/prepare_public_batches.py
```

---

## Compared systems

Official implementations are used as released; they are not copied into this
repository. Each worker in `eval/tools/` calls the tool's own driver on one pair.

| System | Venue | Code and version |
|---|---|---|
| SQLWitness | SIGMOD'26 | Zenodo [10.5281/zenodo.23396996](https://doi.org/10.5281/zenodo.23396996), `sqlwitness-code 0.1.0` |
| ParSEval | PVLDB'25 | [github.com/sfu-db/ParSEval](https://github.com/sfu-db/ParSEval) `v1.0.1` (`88cfe245bcf249a5b0df561a6b09c116ff2a2896`) |
| XDataN | SIGMOD'26 | [gitlab.com/xdata/xdatapublic](https://gitlab.com/xdata/xdatapublic) `a860311` |
| SQLSolver (prover) | SIGMOD'24 | [github.com/SJTU-IPADS/SQLSolver](https://github.com/SJTU-IPADS/SQLSolver) `dcc2a91d8971a4c4d30b055f99d7d8428a1b754b` |
| Polygon (encoding base) | PLDI'25 | Zenodo [10.5281/zenodo.15048096](https://doi.org/10.5281/zenodo.15048096), vendored under `vendor/polygon/` |
| VeriEQL | OOPSLA'24 | [github.com/VeriEQL/VeriEQL](https://github.com/VeriEQL/VeriEQL) |
| SQLDriller components | SIGMOD'25 | SQLSolver above + [TestSuiteEval](https://github.com/ruiqi-zhong/TestSuiteEval) `7bf6378`; install with `vendor/sqldriller/setup_server.sh` |

Install XDataN with `vendor/xdatan/setup_server.sh` (binds PostgreSQL to
127.0.0.1 and generates a local password).

---

## Reproducing the main comparison

The main comparison runs every method on the same workload, adjacently on one
pinned physical core, with the compared tool's per-pair limit, a 600 s workload
budget and five arrival orders (seeds 0-4).

```bash
# one batch, one method (SEER or a compared tool)
python scripts/run_stream_batch.py --help          # PostgreSQL / SQLite batches
python scripts/run_stream_batch_sqlite.py --help   # LeetCode on in-process SQLite

# frozen matrix: formal panels, ablation, BIRD pool
cd experiments/c69
OUT="$SEER_ROOT/repro/c69-production"
python production.py "$OUT"          # stages formal, ablation, bird under one memory budget
python summarize_formal.py --formal "$OUT/formal" --ablation "$OUT/ablation" \
    --legacy-csv reference-table3-leetcode-sqlite-m36.csv --replay "$OUT/replay" --out "$OUT/tables"
python summarize_xdata.py --formal "$OUT/formal" --replay "$OUT/replay" --out "$OUT/tables"
python score_bird_c69.py "$OUT/bird" "$OUT/tables"
```

`experiments/c69/inputs/formal-jobs-c64-class-v5.json` lists the 700 formal
units (panel, batch, seed, input file and SHA-256); `freeze-c69.json` records the
frozen commit. Memory admission keeps the sum of worker limits plus 2 GiB below
half of `MemAvailable`.

---

## Status of this supplement

- The drivers in `experiments/c69/` and many `scripts/` read inputs and earlier
  run outputs from the workspace layout under `$SEER_ROOT/repro/` that produced
  the paper (job lists, certified-database adapters, earlier reference tables).
  They document exactly how the reported numbers were produced, but a
  self-contained one-command reproduction has not yet been packaged.
- The Calcite and Literature panels and the Spider/BIRD reproduction rows came
  from earlier frozen builds of the same code base; their commits are not part
  of this snapshot.
- The LLM-generated TPC-H rewrite files of the early prototype (`data/rewrites.jsonl`,
  `data/seer2_tpch_batch.jsonl`) are not included; `eval/build/gen_rewrites.py` regenerates them.
- No license has been chosen for the SEER code yet. `vendor/polygon/` remains
  under CC-BY-4.0 by its authors.
