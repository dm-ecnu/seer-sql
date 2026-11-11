"""SQLDriller's hybrid SQL equivalence checker (Yang, Wang, Xia, Wei, Ding, Piskac, Chen, Li, "Automated Validating
and Fixing of Text-to-SQL Translation with Execution Consistency", SIGMOD 2025, PACMMOD 3(3) Art. 134,
doi 10.1145/3725271). The authors released no code; this is an implementation from the paper, built only from the
released components the paper names.

Paper -> code
  Sec. 4.1 "Design of a Hybrid Checker", Fig. 8, run on one pair <q1, q2> (`check`):
    (1) SQLSolver [SIGMOD'24] first. EQ -> return EQ.  NEQ / UNKNOWN -> continue.            `_sqlsolver`
        Released SQLSolver (github.com/SJTU-IPADS/SQLSolver @ dcc2a91, Apache-2.0), its CLI
        `java -jar sqlsolver.jar -sql1 -sql2 -schema`, schema = our DDL. A parse failure (Calcite) or a
        crash is SQLSolver's UNKNOWN, a kill at its stage cap is TIMEOUT; both continue, as Fig. 8 does.
    (2) VeriEQL [OOPSLA'24] with Bound := 1, Bound += 1 while the round says EQ, up to K = 10 ("empirically
        set as 10"). A counterexample -> return (NEQ, cex).                                   `_verieql`
        Our VeriEQL install (v1.0 @ 3b99928) through eval/tools/verieql_worker.py, unchanged, one call per
        bound, so the bound loop is exactly Fig. 8's.
    (3) Test Suite Execution [Zhong et al., EMNLP'20] "as a backup for timeouts or unreliable verification
        results": run both queries on a series of generated database instances; the first instance on which
        the results differ is returned as the counterexample, otherwise EQ.                    `_testsuite`
        Released generator (github.com/ruiqi-zhong/TestSuiteEval @ 7bf6378, `fuzz.fuzz.DBFuzzer`, values
        from the queries' comparisons by `sql_util.parse` + `sql_util.value_typing`, as its
        `generate_random_db_with_queries_wrapper`), seeded per instance; execution on the dataset's engine.
    Reaching (3): VeriEQL EQ at every bound 1..K, a VeriEQL timeout / unsupported / error, or a VeriEQL
    counterexample that our shared validator rejects (constraint-violating or not separating on the engine:
    the paper's "unreliable verification results").
  "each time two SQLs come, the checker first runs them on existing generated counterexamples": the
    counterexample cache. Not in this worker: eval/seereval/shell.py `pairwise_shell` runs every new query on
    all cached databases first (signatures) and calls the checker only when they agree.
  Sec. 4.1 "Generating Preliminary SQL Candidates" (LLM prompt) and "Making Counterexamples Practical" (extra
    CHECK constraints so an LLM can simulate the NL query): out of scope, there is no NL side in our task;
    candidates come from the dataset.

Not fixed by the paper, set here: one per-call budget `timeout` covers all three stages. SQLSolver gets at most
SQLSOLVER_SHARE of it; VeriEQL rounds run while more than TESTSUITE_RESERVE of it is left, so the backup stage
always runs; the test suite uses the rest, at most TESTSUITE_MAX_DBS instances, in a child process killed at
the deadline. Instances are checked with eval/seereval/validator.py (constraint-valid and differing results), the
same check the shell applies.
Generator adaptation: the released fuzzer targets Spider schemas, which have no composite foreign keys and only
small integers and unsized strings. Each fuzzed instance gets two repairs before the check (`_repair`): every
composite FK tuple of a child row is replaced by a random parent key tuple, and values are fitted to the declared
column size (INT folded into 32 bits, CHAR/VARCHAR(n) truncated, DECIMAL(p, s) folded below 10^(p-s)), a value
outside an enum (CHECK c IN (...)) is replaced by a random member, a value violating a single-column comparison
CHECK or range domain is moved to the nearest allowed value, and rows that then repeat a primary key are dropped.
Without them almost no TPC-H-style or XData-BM instance loads.

Request: VeriEQL's request {tables, constraints, q1, q2, dialect, timeout} plus `schema` (Schema.to_json), `ddl`
(SQLSolver schema) and `seed`. Optional `stages` (subset of ["sqlsolver", "verieql", "testsuite"]).
Response: verdict (neq + instance | eq | timeout | unsupported | error), `stage` (sqlsolver_eq, verieql_neq@k,
testsuite_neq, testsuite_eq), `stage_secs`, `sqlsolver` (its raw answer), `verieql_bounds` (per-bound verdicts),
`testsuite_dbs` (instances tried).
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import json, os, random, re, sqlite3, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.dirname(HERE))  # eval/, for seereval
from wproto import serve  # noqa: E402

SEER = os.environ.get("SEER_TOOLS_ROOT", (SEER_ROOT))
SD = os.environ.get("SD_ROOT", f"{SEER}/sqldriller")
K = 10
SQLSOLVER_SHARE = 0.25
TESTSUITE_RESERVE = 0.25
TESTSUITE_MAX_DBS = 1000


# ---------------------------------------------------------------- stage 1: SQLSolver
def _sqlsolver(r, cap):
    """-> 'EQ' | 'NEQ' | 'UNKNOWN' | 'TIMEOUT'"""
    lib = f"{SD}/SQLSolver/lib"
    with tempfile.TemporaryDirectory(prefix="sqlsolver_") as d:
        for name, text in (("q1.sql", r["q1"]), ("q2.sql", r["q2"]), ("schema.sql", r["ddl"])):
            with open(os.path.join(d, name), "w") as f:
                f.write(" ".join(text.split()) + "\n" if name != "schema.sql" else text)
        cmd = [f"{SD}/jdk17/bin/java", "-Xmx3g", "-XX:+UseSerialGC", f"-Djava.library.path={lib}", "-jar",
               f"{SD}/SQLSolver/build/libs/sqlsolver.jar", "-sql1=q1.sql", "-sql2=q2.sql", "-schema=schema.sql",
               "-output=out.txt"]
        env = dict(os.environ, LD_LIBRARY_PATH=lib)
        try:
            subprocess.run(cmd, cwd=d, env=env, timeout=max(1.0, cap), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return "TIMEOUT"
        try:
            ans = open(os.path.join(d, "out.txt")).read().split()
        except OSError:
            return "UNKNOWN"
    return ans[0] if ans and ans[0] in ("EQ", "NEQ", "UNKNOWN", "TIMEOUT") else "UNKNOWN"


# ---------------------------------------------------------------- stage 2: VeriEQL, one bound
def _verieql(r, bound, cap):
    req = {k: r[k] for k in ("tables", "constraints", "q1", "q2", "dialect")}
    req.update(timeout=cap, bound=bound)
    env = dict(os.environ, PYTHONPATH=f"{SEER}/verieql", MALLOC_ARENA_MAX="2")
    try:
        p = subprocess.run([f"{SEER}/venv-veq/bin/python", os.path.join(HERE, "verieql_worker.py")],
                           input=json.dumps(req, default=str), capture_output=True, text=True, env=env,
                           cwd=f"{SEER}/verieql", timeout=cap + min(15.0, 0.1 * float(r["timeout"])))
    except subprocess.TimeoutExpired:
        return {"verdict": "timeout", "reason": "hard_timeout"}
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"verdict": "error", "reason": (p.stderr or "")[-200:]}


# ---------------------------------------------------------------- shared validator
def _schema(r):
    from seereval.schema import Schema
    return Schema.from_json(r["schema"])


def _genuine(r, inst, timeout_s=30.0):
    from seereval.validator import check_db, classify_from_check
    chk = check_db(_schema(r), r["dialect"], inst, {"a": r["q1"], "b": r["q2"]}, timeout_s, also_bare=False)
    return classify_from_check(chk, "a", "b")["class"] == "genuine"


# ---------------------------------------------------------------- stage 3: test-suite execution
_TS_TYPE = {"int": "INTEGER", "real": "REAL", "numeric": "NUMERIC", "varchar": "TEXT", "date": "DATE",
            "timestamp": "DATETIME", "time": "TIME", "boolean": "BOOLEAN"}


def _fuzz_sqlite(s, path):
    """Empty SQLite database in the layout TestSuiteEval's schema reader expects (one column per line; single-column
    UNIQUE inline; PK and single-column FKs as table constraints; a composite FK becomes per-column references,
    which is how its reader treats them)."""
    q = lambda x: '"' + x.replace('"', '""') + '"'
    con = sqlite3.connect(path)
    for t in s.tables:
        uniq = {u[0].lower() for u in t.unique if len(u) == 1}
        lines = [f"{q(c['name'])} {_TS_TYPE.get(c['type'], 'TEXT')}{' unique' if c['name'].lower() in uniq else ''}"
                 for c in t.columns]
        if t.primary_key:
            lines.append("primary key (" + ", ".join(q(c) for c in t.primary_key) + ")")
        for fk in t.foreign_keys:
            for c, pc in zip(fk["columns"], fk["ref_columns"]):
                lines.append(f"foreign key ({q(c)}) references {q(fk['ref_table'])}({q(pc)})")
        con.execute(f"create table {q(t.name)} (\n" + ",\n".join(lines) + "\n)")
    con.commit()
    con.close()


INT_LO, INT_HI = -2 ** 31, 2 ** 31 - 1


def _fit(c):
    """Value -> value that fits the column's declared size (INT 32 bit, CHAR/VARCHAR(n), DECIMAL(p, s))."""
    st = (c.get("sql_type") or "").upper()
    m = re.match(r"\s*(?:VAR)?CHAR(?:ACTER)?(?:\s+VARYING)?\s*\(\s*(\d+)\s*\)", st)
    if m:
        n = int(m.group(1))
        return lambda v: v[:n] if isinstance(v, str) else v
    m = re.match(r"\s*(?:DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*(?:,\s*(\d+))?\s*\)", st)
    if m:
        lim = 10 ** (int(m.group(1)) - int(m.group(2) or 0))
        return lambda v: v % lim if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) >= lim else v
    if c["type"] == "int":
        return lambda v: v % INT_HI if isinstance(v, int) and not INT_LO <= v <= INT_HI else v
    return None


CMP = re.compile(r'^\s*"?(\w+)"?\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$')


def _bounds(s, t):
    """Column -> [(op, const)] of the table's single-column comparison CHECKs and range domains."""
    out = {}
    for ck in s.checks:
        m = CMP.match(ck["expr"]) if ck["table"].lower() == t.name.lower() else None
        if m:
            v = float(m.group(3))
            out.setdefault(m.group(1).lower(), []).append((m.group(2), int(v) if v.is_integer() else v))
    for d in s.domains:
        if d["table"].lower() == t.name.lower():
            for op, k in ((">=", "lo"), ("<=", "hi")):
                if d.get(k) is not None:
                    out.setdefault(d["column"].lower(), []).append((op, d[k]))
    return out


def _clamp(v, rules):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return v
    for op, k in rules:
        if op == ">" and not v > k:
            v = k + 1
        elif op == ">=" and not v >= k:
            v = k
        elif op == "<" and not v < k:
            v = k - 1
        elif op == "<=" and not v <= k:
            v = k
    return v


def _repair(s, inst, rng):
    """Fit values to declared column sizes, enums and single-column comparison CHECKs / domains; drop rows that
    then repeat a primary key; give each child row of a composite FK one existing parent key tuple."""
    rows = {k.lower(): v for k, v in inst.items()}
    for t in s.tables:
        fits = [(c["name"], f) for c in t.columns for f in [_fit(c)] if f]
        enums = {e["column"].lower(): e["values"] for e in s.enums if e["table"].lower() == t.name.lower()}
        bounds = _bounds(s, t)
        for row in rows.get(t.name.lower(), []):
            for c, f in fits:
                if c in row:
                    row[c] = f(row[c])
            for c in row:
                lc = c.lower()
                if lc in enums and row[c] is not None and row[c] not in enums[lc]:
                    row[c] = rng.choice(enums[lc])
                if lc in bounds:
                    row[c] = _clamp(row[c], bounds[lc])
        if t.primary_key and t.name.lower() in rows:
            seen, keep = set(), []
            for row in rows[t.name.lower()]:
                k = tuple(row.get(c) for c in t.primary_key)
                if k not in seen:
                    seen.add(k)
                    keep.append(row)
            rows[t.name.lower()][:] = keep
    for t in s.tables:
        for fk in t.foreign_keys:
            if len(fk["columns"]) < 2:
                continue
            parents = [tuple(p.get(c) for c in fk["ref_columns"]) for p in rows.get(fk["ref_table"].lower(), [])]
            parents = [p for p in parents if None not in p]
            for row in rows.get(t.name.lower(), []):
                if parents and any(row.get(c) is not None for c in fk["columns"]):
                    row.update(zip(fk["columns"], rng.choice(parents)))
    return inst


def _testsuite(r, cap, genuine=None, max_dbs=TESTSUITE_MAX_DBS):
    """-> (instance or None, number of instances tried). The loop runs in a forked child, killed at `cap`: one
    instance's load and execution is not interruptible from inside."""
    import multiprocessing as mp
    import signal
    ctx = mp.get_context("fork")
    n = ctx.Value("i", 0)
    rx, tx = ctx.Pipe(duplex=False)

    def child():
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))  # SystemExit: engines drop their schemas
        try:
            tx.send(("ok", _testsuite_loop(r, cap, genuine or _genuine, max_dbs, n)))
        except Exception as e:  # noqa: BLE001 - the stage then answers `error`
            tx.send(("error", f"{type(e).__name__}: {e}"[:300]))

    p = ctx.Process(target=child, daemon=True)
    p.start()
    tx.close()
    try:
        tag, inst = rx.recv() if rx.poll(cap) else ("ok", None)
    except EOFError:
        tag, inst = "ok", None
    if p.is_alive():
        p.terminate()
        p.join(3)  # SIGTERM lands between calls; a call blocked in the driver gets SIGKILL, its schema may stay
        if p.is_alive():
            p.kill()
    p.join()
    if tag == "error":
        raise RuntimeError(inst)
    return inst, n.value


def _testsuite_loop(r, cap, genuine, max_dbs, n):
    end = time.time() + cap
    ts = f"{SD}/TestSuiteEval"
    if ts not in sys.path:
        sys.path.insert(0, ts)
    from fuzz.fuzz import DBFuzzer
    from sql_util.parse import extract_typed_value_in_comparison_from_query
    from sql_util.value_typing import type_values_w_db
    s = _schema(r)
    names = {t.name.lower(): (t.name, {c["name"].upper(): c["name"] for c in t.columns}) for t in s.tables}
    with tempfile.TemporaryDirectory(prefix="testsuite_") as d:
        path = os.path.join(d, "empty.sqlite")
        _fuzz_sqlite(s, path)
        typed = []
        for sql in (r["q1"], r["q2"]):
            try:
                typed += extract_typed_value_in_comparison_from_query(sql)
            except Exception:  # the released parser targets Spider SQL; no constants then
                pass
        try:
            values = type_values_w_db(path, typed, loose=True)
        except Exception:
            values = type_values_w_db(path, [], loose=True)
        i = 0
        while i < max_dbs and time.time() < end:
            random.seed(f"{r.get('seed', 0)}:{i}")
            i += 1
            tables = DBFuzzer(path, values).get_fuzz()
            inst = {}
            for tn, cols in tables.items():
                orig, cmap = names[tn.lower()]
                keys = list(cols)
                rows = len(cols[keys[0]]) if keys else 0
                inst[orig] = [{cmap.get(k, k): _plain(cols[k][j]) for k in keys} for j in range(rows)]
            _repair(s, inst, random)
            left = end - time.time()
            if left < 0.5:
                break
            hit = genuine(r, inst, timeout_s=min(30.0, left))
            n.value = i
            if hit:
                return inst
    return None


def _plain(v):
    if v is None or isinstance(v, (int, float, str)):
        return v
    return str(v)


# ---------------------------------------------------------------- Fig. 8
def check(r, sqlsolver=_sqlsolver, verieql=_verieql, testsuite=_testsuite, genuine=_genuine, clock=time.time):
    T = float(r["timeout"])
    t0 = clock()
    deadline = t0 + T
    stages = set(r.get("stages") or ("sqlsolver", "verieql", "testsuite"))
    out = {"stage_secs": {}, "verieql_bounds": []}

    if "sqlsolver" in stages:
        t = clock()
        ans = sqlsolver(r, min(T * SQLSOLVER_SHARE, deadline - t))
        out["stage_secs"]["sqlsolver"] = round(clock() - t, 3)
        out["sqlsolver"] = ans
        if ans == "EQ":
            return {**out, "verdict": "eq", "stage": "sqlsolver_eq"}

    if "verieql" in stages:
        t = clock()
        for k in range(1, K + 1):
            left = deadline - clock() - T * TESTSUITE_RESERVE
            if left < 1:
                out["verieql_bounds"].append([k, "no_budget"])
                break
            v = verieql(r, k, left)
            out["verieql_bounds"].append([k, v.get("verdict")])
            if v.get("verdict") == "neq" and v.get("instance") is not None:
                if genuine(r, v["instance"]):
                    out["stage_secs"]["verieql"] = round(clock() - t, 3)
                    return {**out, "verdict": "neq", "instance": v["instance"], "stage": f"verieql_neq@{k}"}
                out["verieql_bounds"][-1][1] = "neq_unreliable"
                break
            if v.get("verdict") != "eq":
                break
        out["stage_secs"]["verieql"] = round(clock() - t, 3)

    if "testsuite" in stages:
        t = clock()
        left = deadline - t
        try:
            inst, n = testsuite(r, max(0.0, left)) if left > 0.5 else (None, 0)
        except RuntimeError as e:
            out["stage_secs"]["testsuite"] = round(clock() - t, 3)
            return {**out, "verdict": "error", "stage": "testsuite_error", "reason": str(e)}
        out["stage_secs"]["testsuite"] = round(clock() - t, 3)
        out["testsuite_dbs"] = n
        if inst is not None:
            return {**out, "verdict": "neq", "instance": inst, "stage": "testsuite_neq"}
        if n == 0:
            return {**out, "verdict": "timeout", "stage": "testsuite_none", "reason": "no test-suite instance checked within the budget"}
    return {**out, "verdict": "eq", "stage": "testsuite_eq"}


if __name__ == "__main__":
    serve(check)
