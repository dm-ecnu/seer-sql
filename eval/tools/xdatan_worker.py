"""XDataN (Somwase, Das, Sudarshan, "Test Data Generation for Complex SQL Queries", SIGMOD'26) on one query and
its mutants, through the authors' own driver test.RegressionTests (xdata.jar, gitlab.com/xdata/xdatapublic @a860311;
the repo has no LICENSE file). Adapter only: XDataN generates its datasets for the query `q1`, then its own
kill test runs every mutant on them; we return every generated dataset (DS0, DS1, ...) as a candidate
counterexample database, plus XDataN's own per-mutant state. The shared validator decides which datasets are
genuine (XDataN counts a mutant that raises an SQL error as killed, RegressionTests.testMutantKilling; we do not).

Request: {ddl, q1, mutants: [sql, ...], tables: {table: [column, ...]}, types: {table: {column: type}},
          timeout, xmx}
Response: verdict neq (some dataset generated) | unsupported (XDataN generated no dataset: its own
"Unsupported") | timeout | error; instances: [{table: [{col: val}]}], states: [Killed|NotKilled|Unsupported|None]
per mutant, n_datasets.

Runs on eval-server in $XD_ROOT (vendor/xdatan/setup_server.sh): one PostgreSQL database per call (XDataN lists
every temporary table of its database, so concurrent calls must not share one) and a private /tmp per call
(XDataN writes the hard-coded scratch file /tmp/dummy), via an unprivileged user+mount namespace.
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import os
import re
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wproto import serve  # noqa: E402

XD = os.environ.get("XD_ROOT", (SEER_ROOT + "/xdatan"))
# per-call scratch on tmpfs: XDataN writes many small files (datasets, SMT files, logs) and the /data disk on server
# is a shared spinning disk that other jobs saturate (2026-10-21: 97% util, 300 ms write latency)
CALLS = os.environ.get("XD_CALLS", "/dev/shm/seer-xdatan/calls")


def one_line(sql: str) -> str:
    """XDataN reads one `id|type|sql` entry per line."""
    parts = re.split(r"('(?:''|[^'])*')", sql)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"--[^\n]*", " ", parts[i])
        parts[i] = re.sub(r"\s+", " ", parts[i])
    return "".join(parts).strip().rstrip(";").strip()


def split_values(s: str):
    """`'a','b',null,3` -> ['a', 'b', None, '3'] (quotes with '' escapes; unquoted null)."""
    out, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i] in " \t,":
            i += 1
        if i >= n:
            break
        if s[i] == "'":
            j, buf = i + 1, []
            while j < n:
                if s[j] == "'" and j + 1 < n and s[j + 1] == "'":
                    buf.append("'"); j += 2
                elif s[j] == "'":
                    break
                else:
                    buf.append(s[j]); j += 1
            out.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and s[j] != ",":
                j += 1
            tok = s[i:j].strip()
            out.append(None if tok.lower() == "null" else tok)
            i = j
    return out


def cast(v, ty):
    if v is None:
        return None
    if ty in ("int", "boolean"):
        try:
            f = float(v)
            return int(f) if f == int(f) else f
        except ValueError:
            return v
    if ty in ("real", "numeric"):
        try:
            return float(v)
        except ValueError:
            return v
    return v


def parse_dataset(path, tables, types):
    """One DS*.sql file (`delete from T` / `insert into T values (...)` / `-- ...`) -> {table: [{col: val}]}."""
    low = {t.lower(): t for t in tables}
    inst = {t: [] for t in tables}
    for line in open(path):
        m = re.match(r"\s*insert\s+into\s+([\w.\"]+)\s+values\s*\((.*)\)\s*;?\s*$", line, re.I)
        if not m:
            continue
        t = low.get(m.group(1).strip('"').split(".")[-1].lower())
        if t is None:
            continue
        vals = split_values(m.group(2))
        cols = tables[t]
        # XDataN drops empty-string values from its INSERT lists (PopulateTestData: `if (!k.isEmpty())`), and
        # PostgreSQL, where XDataN loads them itself, fills the missing trailing columns with their defaults (NULL):
        # read the row as PostgreSQL does, so the database is the one XDataN tested its mutants on
        if len(vals) < len(cols):
            vals = vals + [None] * (len(cols) - len(vals))
        if len(vals) != len(cols):
            raise ValueError(f"{os.path.basename(path)}: {t} has {len(cols)} columns, row has {len(vals)}")
        inst[t].append({c: cast(v, types[t].get(c)) for c, v in zip(cols, vals)})
    return inst


def handle(r):
    t0 = time.time()
    call = os.path.join(CALLS, f"{os.getpid()}-{int(t0 * 1000)}")
    bench, work = os.path.join(call, "bm"), os.path.join(call, "w")
    os.makedirs(bench)
    os.makedirs(os.path.join(call, "privtmp"))
    try:
        open(os.path.join(bench, "DDL.sql"), "w").write(r["ddl"].rstrip().rstrip(";") + ";\n")
        open(os.path.join(bench, "queries.txt"), "w").write(f"1|q|{one_line(r['q1'])}\n")
        open(os.path.join(bench, "mutants.txt"), "w").write(
            "".join(f"1|m|{one_line(m)}\n" for m in r["mutants"]))
        env = dict(os.environ, LD_LIBRARY_PATH=os.path.join(XD, "src/xdata/z3"), XD_DB=f"c{os.getpid()}",
                   XD_XMX=str(r.get("xmx", "3g")),
                   # virtual reservations kept small so the JVM starts under the per-process address-space cap
                   XD_JVM_OPTS="-XX:ReservedCodeCacheSize=128m -XX:MaxMetaspaceSize=256m "
                               "-XX:CompressedClassSpaceSize=128m -Xss1m")
        cmd = ["unshare", "-Urm", "sh", "-c",
               f"mount --bind '{call}/privtmp' /tmp && exec '{XD}/venv/bin/python' -I '{XD}/xd_run.py' "
               f"'{bench}' '{work}'"]
        p = subprocess.Popen(cmd, env=env, stdout=sys.stderr, stderr=sys.stderr)
        timed_out = False
        try:
            p.wait(timeout=max(1.0, r["timeout"] - 2.0 - (time.time() - t0)))  # 2 s to read the datasets
        except subprocess.TimeoutExpired:
            timed_out = True
            p.send_signal(signal.SIGKILL)
            p.wait()
        xd_secs = time.time() - t0
        # datasets written so far count, also after a timeout (XDataN writes them one by one)
        ds_dir = os.path.join(work, "tmp", "temp_smt", "1")
        files = sorted((f for f in os.listdir(ds_dir) if re.match(r"DS\d+\.sql$", f)) if os.path.isdir(ds_dir) else [],
                       key=lambda f: int(f[2:-4]))
        insts, bad = [], {}
        for f in files:       # a dataset we cannot read is dropped (counted), the others are still returned
            try:
                insts.append(parse_dataset(os.path.join(ds_dir, f), r["tables"], r["types"]))
            except ValueError as e:
                bad[f] = str(e)[:200]
        states = [None] * len(r["mutants"])
        outf = os.path.join(work, "output", "bm-mutant-results.out")
        if os.path.exists(outf):  # lines `1|query\t:\tmutant:\t state='X'` (not written for every mutant)
            got = {}
            for line in open(outf):
                m = re.match(r"^\d+\|.*?\t:\t(.*):\t state='(\w+)'\s*$", line)
                if m:
                    got[m.group(1).strip()] = m.group(2)
            states = [got.get(one_line(m)) for m in r["mutants"]]
        if insts:
            verdict = "neq"
        elif timed_out:
            verdict = "timeout"
        elif any(s == "Unsupported" for s in states) or p.returncode == 0:
            verdict = "unsupported"
        else:
            verdict = "error"
        return {"verdict": verdict, "instances": insts, "states": states, "n_datasets": len(insts),
                "datasets": files, "unreadable": bad or None, "reason": ("timeout" if timed_out else None) or
                ("" if p.returncode == 0 else f"rc={p.returncode}"), "xd_secs": round(xd_secs, 3)}
    finally:
        # cleanup is detached so that it does not count against the caller's budget
        psql = os.path.join(os.environ.get("XD_PGBIN", SEER_ROOT + "/venv-pgsrv/lib/"
                                           "python3.10/site-packages/pgserver/pginstall/bin"), "psql")
        drop = f'DROP DATABASE IF EXISTS "xd_c{os.getpid()}" WITH (FORCE)'
        try:
            subprocess.Popen(["sh", "-c", f"timeout 60 '{psql}' -h '{XD}' -p {os.environ.get('XD_PORT', '55930')} "
                                          f"-U postgres -qtA postgres -c '{drop}'; rm -rf '{call}'"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception:
            shutil.rmtree(call, ignore_errors=True)

if __name__ == "__main__":
    serve(handle)
