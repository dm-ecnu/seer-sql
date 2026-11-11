"""Baseline tools behind one call: build the tool's native input from the normalized schema, run the tool's
adapter (eval/tools/<tool>_worker.py) in the tool's own venv as a child process under `prlimit --as` and a hard
wall-clock limit, and return a normalized verdict:

  neq          the tool returned a counterexample database (`instance`, our {table: [{col: val}]} format)
  eq           the tool finished without one (VeriEQL: bounded equivalence; SQLWitness/ParSEval: search ended)
  timeout      the tool's own time limit, or our hard limit (`hard_timeout`)
  unsupported  the tool rejects the query/schema (parse or encoding failure)
  error        crash, memory limit, or an unparseable response

Paths are fixed to the eval-server layout under $SEER_TOOLS_ROOT (default $SEER_ROOT).
"""
from __future__ import annotations

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import contextlib, hashlib, json, os, re, signal, subprocess, sys, tempfile, threading, time
from typing import Any, Dict, List

from .engine import normalize_sql
from .schema import Schema, to_ddl, to_sqlwitness

ROOT = os.environ.get("SEER_TOOLS_ROOT", (SEER_ROOT))
WORKERS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
TOOLS = {
    "sqlwitness": {"py": f"{ROOT}/repro/sqlwitness/venv-sw-py311-pinned/bin/python", "worker": "sqlwitness_worker.py", "pythonpath": None,
                   "cwd": None, "mem_gb": 8},
    "parseval": {"py": f"{ROOT}/venv-pv/bin/python", "worker": "parseval_worker.py", "pythonpath": None,
                 "cwd": None, "mem_gb": 8},
    "polygon": {"py": f"{ROOT}/venv-pg/bin/python", "worker": "polygon_worker.py",
                "pythonpath": f"{ROOT}/polygon/src/polygon-artifact-evaluation", "cwd": None, "mem_gb": 16},
    "verieql": {"py": f"{ROOT}/venv-veq/bin/python", "worker": "verieql_worker.py", "pythonpath": f"{ROOT}/verieql",
                "cwd": f"{ROOT}/verieql", "mem_gb": 8},
    "xdatan": {"py": f"{ROOT}/xdatan/venv/bin/python", "worker": "xdatan_worker.py", "pythonpath": None,
               "cwd": None, "mem_gb": 8},
    "sqldriller_official": {"py": f"{ROOT}/sqldriller/venv-official/bin/python", "worker": "sqldriller_official_worker.py", "pythonpath": None,
                            "cwd": None, "mem_gb": 8, "external_cpu": True},
    # SQLDriller's hybrid checker (SIGMOD'25), implemented from the paper: eval/tools/sqldriller_worker.py
    "sqldriller": {"py": f"{ROOT}/sqldriller/venv/bin/python", "worker": "sqldriller_worker.py", "pythonpath": None,
                   "cwd": None, "mem_gb": 8},
}
PARSEVAL_DIALECT = {"sqlite": "sqlite", "postgresql": "postgres", "mysql": "mysql"}
VERIEQL_TYPE = {"int": "INT", "real": "INT", "numeric": "INT", "boolean": "INT", "varchar": "VARCHAR",
                "time": "VARCHAR", "date": "DATE", "timestamp": "DATE"}


def _pg_url():
    return os.environ.get("SEER_PG_URL", "postgresql://probe@127.0.0.1:55917/postgres")


def _mysql_url():
    u = os.environ.get("SEER_MYSQL_URL", "mysql://root@127.0.0.1:23306/")
    return u.replace("mysql://", "mysql+pymysql://", 1)


def verieql_input(s: Schema):
    """VeriEQL's schema dict and constraint list: PK and UNIQUE -> primary, single-column FK -> foreign (a
    composite FK becomes one single-column inclusion per column, as for Polygon), NOT NULL -> not_null, enums -> in,
    domains -> between. Comparison CHECKs and inclusions are not passed (counted in `omitted_constraints`)."""
    tables = {t.name.upper(): {c["name"].upper(): VERIEQL_TYPE[c["type"]] for c in t.columns} for t in s.tables}
    v = lambda t, c: {"value": f"{t.upper()}__{c.upper()}"}
    cons, omitted = [], 0
    for t in s.tables:
        for key in ([t.primary_key] if t.primary_key else []) + list(t.unique):
            cons.append({"primary": [v(t.name, c) for c in key]})
        for fk in t.foreign_keys:
            for c, pc in zip(fk["columns"], fk["ref_columns"]):
                cons.append({"foreign": [v(t.name, c), v(fk["ref_table"], pc)]})
        for c in t.columns:
            if c.get("not_null") and c["name"] not in t.primary_key:
                cons.append({"not_null": v(t.name, c["name"])})
    lit = lambda x: x if isinstance(x, (int, float)) or re.match(r"^\d{4}-\d{2}-\d{2}$", str(x)) \
        else {"literal": str(x)}
    for e in s.enums:  # VeriEQL `in` (it also makes the column NOT NULL, its own semantics)
        cons.append({"in": [v(e["table"], e["column"]), [lit(x) for x in e["values"]]]})
    for d in s.domains:
        cons.append({"between": [v(d["table"], d["column"]), lit(d["lo"]), lit(d["hi"])]})
    omitted = len(s.checks) + len(s.inclusions)
    return tables, cons, omitted


def sqlite_dq_literals(sql: str, s: Schema) -> str:
    """SQLite reads "x" as a string literal when no column/alias x exists (Spider and BIRD use this a lot); the
    tools' SQL parsers read it as an identifier. Rewrite such tokens to 'x' (same SQLite semantics): any "x" that
    is not a table, column or alias name of the query is a literal."""
    names = {t.name.lower() for t in s.tables} | {c["name"].lower() for t in s.tables for c in t.columns}
    names |= {m.lower() for m in re.findall(r'\bas\s+"([^"]+)"', sql, re.I)}
    names |= {m.lower() for m in re.findall(r"\bas\s+([A-Za-z_]\w*)", sql, re.I)}
    parts = re.split(r"('(?:''|[^'])*')", sql)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r'"((?:""|[^"])*)"', lambda m: m.group(0) if m.group(1).lower() in names
                          else "'" + m.group(1).replace('""', '"').replace("'", "''") + "'", parts[i])
    return "".join(parts)


def build_request(tool: str, s: Schema, dialect: str, queries: List[str], timeout: float, seed: int,
                  op: str = "pair") -> Dict[str, Any]:
    if dialect == "sqlite":
        queries = [sqlite_dq_literals(q, s) for q in queries]
    qs = [normalize_sql(q, dialect).strip().rstrip(";").strip() for q in queries]
    if tool == "sqlwitness":
        tables, cons = to_sqlwitness(s)
        return {"tables": tables, "constraints": cons, "q1": qs[0], "q2": qs[1], "dialect": dialect,
                "timeout": timeout, "delta": 0.05, "seed": seed,
                "constraints_yml": "constraints_file" in s.native.get("sqlwitness", {})}
    if tool == "parseval":
        ddl = ";\n".join(to_ddl(s, dialect, deferrable=False)) + ";"
        url = {"sqlite": "sqlite://", "postgresql": _pg_url(), "mysql": _mysql_url()}[dialect]
        return {"ddl": ddl, "q1": qs[0], "q2": qs[1], "dialect": PARSEVAL_DIALECT[dialect], "url": url,
                "timeout": timeout, "seed": seed}
    if tool == "polygon":
        tables, cons = to_sqlwitness(s)
        return {"tables": tables, "constraints": cons, "op": op, "queries": qs, "timeout": timeout}
    if tool in ("sqldriller", "sqldriller_official"):       # VeriEQL's request (its stage 2) + our schema (test suite) + DDL (SQLSolver)
        tables, cons, omitted = verieql_input(s)
        simple = all(n.replace("_", "").isalnum() for t in s.tables for n in [t.name] + [c["name"] for c in t.columns])
        req = {"tables": tables, "constraints": cons, "q1": qs[0], "q2": qs[1], "dialect": dialect,
                "timeout": timeout, "seed": seed, "schema": s.to_json(),
                "ddl": ";\n".join(to_ddl(s, "mysql", deferrable=False, quote=not simple)) + ";",
                "omitted_constraints": omitted}
        if tool == "sqldriller_official":
            req["require_declared_schema"] = True
            schema_key = hashlib.sha256(json.dumps(s.to_json(), sort_keys=True).encode()).hexdigest()
            index = os.path.join(ROOT, "sqldriller/resources/declared/index", schema_key + ".json")
            if dialect == "mysql":
                index = os.path.join(ROOT, "sqldriller/resources/declared/cubes-index-v3", schema_key + ".json")
                req["q1"], req["q2"] = queries[:2]
            if os.path.isfile(index):
                with open(index) as stream:
                    req["resource_view"] = json.load(stream)["resource_view"]
        return req
    if tool == "verieql":
        tables, cons, omitted = verieql_input(s)
        return {"tables": tables, "constraints": cons, "q1": qs[0], "q2": qs[1], "timeout": timeout, "bound": 2,
                "dialect": dialect, "omitted_constraints": omitted}
    if tool == "xdatan":
        return xdatan_request(s, dialect, qs, timeout)
    raise ValueError(tool)


_SIMPLE = re.compile(r"^[a-z_][a-z0-9_]*$")


def xdatan_request(s: Schema, dialect: str, qs: List[str], timeout: float) -> Dict[str, Any]:
    """XDataN runs on PostgreSQL only. Its home benchmarks keep their own DDL file (`native.xdatan_ddl`); other
    schemas get our PostgreSQL DDL (identifiers quoted only when not plain lower-case names), and queries of other
    dialects are transpiled to PostgreSQL with sqlglot (left as they are when that fails; XDataN then reports them
    unsupported). The first query is XDataN's query, the others its mutants."""
    import sqlglot
    ddl = s.native.get("xdatan_ddl")
    if not ddl:
        names = [t.name for t in s.tables] + [c["name"] for t in s.tables for c in t.columns]
        quote = not all(_SIMPLE.match(n) for n in names)
        ddl = ";\n".join(to_ddl(s, "postgresql", deferrable=False, quote=quote)) + ";"
    if dialect != "postgresql":
        out = []
        for q in qs:
            try:
                out.append(sqlglot.transpile(q, read={"sqlite": "sqlite", "mysql": "mysql"}[dialect],
                                             write="postgres")[0])
            except Exception:
                out.append(q)
        qs = out
    return {"ddl": ddl, "q1": qs[0], "mutants": qs[1:], "timeout": timeout,
            "tables": {t.name: [c["name"] for c in t.columns] for t in s.tables},
            "types": {t.name: {c["name"]: c["type"] for c in t.columns} for t in s.tables}}


# CPU accounting: every call adds the CPU seconds of its whole process group (the worker, its solver and helper
# processes) to the calling thread's counter, so a runner can report CPU time per batch next to wall time.
_CPU = threading.local()
_TCK = os.sysconf("SC_CLK_TCK")


def cpu_used() -> float:
    return getattr(_CPU, "v", 0.0)


def incomplete_cpu_calls() -> int:
    return getattr(_CPU, "incomplete", 0)


def runq_used() -> float:
    """Seconds the calling thread's tool workers waited for a CPU (run-queue delay)."""
    return getattr(_CPU, "rq", 0.0)


def _group_cpu(pgid: int):
    """CPU seconds and run-queue delay of the live members of a process group (CPU includes reaped children)."""
    tot, rq = 0.0, 0.0
    for d in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not d.isdigit():
            continue
        try:
            f = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()
            if int(f[2]) != pgid:
                continue
            tot += sum(map(int, f[11:15])) / _TCK
            rq += int(open(f"/proc/{d}/schedstat").read().split()[1]) / 1e9
        except (OSError, ValueError, IndexError):
            continue
    return tot, rq


def call(tool: str, req: Dict[str, Any], hard_s: float, mem_gb: float | None = None) -> Dict[str, Any]:
    cpu_file = tempfile.NamedTemporaryFile(prefix="cpu_", suffix=".txt", delete=False).name
    try:
        with contextlib.ExitStack() as cleanup:
            worker_cwd = None
            if tool == "sqlwitness" and req.get("dialect") == "sqlite":
                # The released tool creates test.db in its working directory.
                # Keep its SQLite I/O on the same local memory filesystem used
                # for the reproduced original benchmark.
                scratch = os.environ.get("SEER_SQLWITNESS_SQLITE_SCRATCH", "/dev/shm")
                worker_cwd = cleanup.enter_context(
                    tempfile.TemporaryDirectory(prefix="sqlwitness-", dir=scratch))
            if tool == "sqldriller_official":
                parent = os.path.join(ROOT, "repro/official/call_workspaces")
                os.makedirs(parent, exist_ok=True)
                workspace = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="call-", dir=parent))
                req = dict(req, _call_workspace=workspace)
            resp = _call(tool, req, hard_s, mem_gb, cpu_file, worker_cwd)
    finally:
        try:
            os.unlink(cpu_file)
        except OSError:
            pass
    _CPU.incomplete = incomplete_cpu_calls() + int(not resp.get("cpu_complete", True))
    _CPU.v = cpu_used() + resp.get("cpu", 0.0)
    _CPU.rq = runq_used() + resp.get("runq", 0.0)
    return resp


def _call(tool, req, hard_s, mem_gb, cpu_file, worker_cwd=None):
    cfg = TOOLS[tool]
    # The official checker has no native total-budget argument. Its unchanged
    # algorithm must not consume the legacy startup/cleanup grace as search time.
    if tool == "sqldriller_official":
        hard_s = min(hard_s, max(0.001, float(req["timeout"])))
        req = dict(req, _deadline_monotonic=time.monotonic() + hard_s)
    env = dict(os.environ)
    if cfg["pythonpath"]:
        env["PYTHONPATH"] = cfg["pythonpath"]
    env.setdefault("MALLOC_ARENA_MAX", "2")
    env["PATH"] = os.path.dirname(cfg["py"]) + os.pathsep + env.get("PATH", "")  # Polygon runs the venv's z3 binary
    mem = int((mem_gb or cfg["mem_gb"]) * 2 ** 30)
    cmd = [sys.executable, os.path.join(WORKERS, "cpu_wrap.py"), cpu_file, "--",
           "prlimit", f"--as={mem}", "--", cfg["py"], os.path.join(WORKERS, cfg["worker"])]
    t = time.time()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env=env, cwd=worker_cwd or cfg["cwd"], start_new_session=True)
    try:
        out, err = p.communicate(json.dumps(req, default=str), timeout=hard_s)
    except subprocess.TimeoutExpired:
        left, lrq = _group_cpu(p.pid)
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.communicate()
        return {"verdict": "timeout", "reason": "hard_timeout", "wall": round(time.time() - t, 2),
                "cpu": round(left, 3), "cpu_complete": not cfg.get("external_cpu", False), "runq": round(lrq, 3)}
    finally:
        left, lrq = _group_cpu(p.pid)  # helpers still alive (never reaped by the worker)
        try:  # tool helper processes (Polygon's multiprocess manager) must not outlive the call
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        resp = json.loads(out)
    except Exception:
        tail = (err or "")[-400:]
        reason = "memory_limit" if "MemoryError" in tail or "bad_alloc" in tail else "crash"
        resp = {"verdict": "error", "reason": f"{reason} rc={p.returncode}: {tail}"}
    if resp.get("verdict") in ("unsupported", "error") and err:
        lines = [l for l in err.splitlines() if re.search(r"Error|error|Exception", l)]
        if lines:
            resp["stderr_tail"] = " | ".join(lines[-3:])[-400:]
            if not resp.get("reason"):
                resp["reason"] = lines[-1][-200:]
    resp["wall"] = round(time.time() - t, 2)
    try:
        c, rq = map(float, open(cpu_file).read().split())
    except (OSError, ValueError):
        c, rq = 0.0, 0.0
    host_cpu = c + left
    external = float(resp.get("docker_cpu_s") or 0)
    resp["cpu"] = round(host_cpu + external, 3)
    resp["runq"] = round(rq + lrq, 3)
    if cfg.get("external_cpu"):
        resp["host_cpu"] = round(host_cpu, 3)
        resp["cpu_complete"] = bool(resp.get("docker_cpu_complete", False))
        resp["cpu_scope"] = "host_plus_docker"
    return resp
