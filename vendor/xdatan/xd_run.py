"""Run XDataN's own driver (test.RegressionTests, xdata.jar @a860311) on one benchmark directory.

Equivalent to the authors' execute_tests.py, with paths parameterised so several runs can coexist:
  * a per-run util/XData.properties is placed *before* xdata.jar on the classpath, so
    Configuration.getResourceAsStream picks it up (homeDir, port, z3 path, credentials);
  * every other property is copied verbatim from the jar's own XData.properties;
  * the hard-coded /tmp/dummy scratch file is isolated by the caller (unshare + bind-mount /tmp).

Usage: python xd_run.py BENCH_DIR WORK_DIR   (BENCH_DIR holds DDL.sql, queries.txt, mutants.txt)
Writes WORK_DIR/output/<bm>.sql-mutant-results.out, the authors' result format.
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import os
import re
import shutil
import sys
import zipfile

XD = os.environ.get("XD_ROOT", (SEER_ROOT + "/xdatan"))
JARS = os.path.join(XD, "src/xdata/jars")


PGBIN = (SEER_ROOT + "/venv-pgsrv/lib/python3.10/site-packages/pgserver/pginstall/bin")


def fresh_db(name: str, port: int) -> None:
    """One database per concurrent run: XDataN's TableMap lists every temporary table visible in the
    database's catalog (including other sessions' pg_temp schemas), so runs sharing a database collide."""
    import subprocess
    psql = [os.path.join(PGBIN, "psql"), "-h", XD, "-p", str(port), "-U", "postgres", "-qtA", "postgres", "-c"]
    subprocess.run(psql + [f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'], check=True)
    subprocess.run(psql + [f'CREATE DATABASE "{name}" OWNER xdatauser'], check=True)


def write_props(work: str, port: int, pw: str, z3: str, db: str) -> str:
    pdir = os.path.join(work, "props")
    os.makedirs(os.path.join(pdir, "util"), exist_ok=True)
    with zipfile.ZipFile(os.path.join(JARS, "xdata.jar")) as z:
        base = z.read("util/XData.properties").decode()
    over = {
        "homeDir": os.path.join(work, "tmp"),
        "logFile": os.path.join(work, "tmp", "XData.log"),
        "databasePort": str(port),
        "databaseIP": "127.0.0.1",
        "databaseName": db,
        "smtsolver": z3,
        "existingDatabaseUserPasswd": pw,
        "testDatabaseUserPasswd": pw,
    }
    out = []
    for line in base.splitlines():
        m = re.match(r"\s*([A-Za-z_][\w.]*)\s*=", line)
        if m and m.group(1) in over:
            line = f"{m.group(1)}={over.pop(m.group(1))}"
        out.append(line)
    out += [f"{k}={v}" for k, v in over.items()]
    p = os.path.join(pdir, "util", "XData.properties")
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    return pdir


def prepare_input(work: str, bench: str, bm: str) -> str:
    base = os.path.join(work, "inputs", bm)
    for d in ("datasets", "queries", "mutants", "sample"):
        os.makedirs(os.path.join(base, d), exist_ok=True)
    shutil.copy(os.path.join(bench, "DDL.sql"), os.path.join(base, "datasets", bm + ".sql"))
    shutil.copy(os.path.join(bench, "queries.txt"), os.path.join(base, "queries", bm + ".txt"))
    shutil.copy(os.path.join(bench, "mutants.txt"), os.path.join(base, "mutants", bm + ".txt"))
    with open(os.path.join(base, "sample", bm + ".json"), "w") as f:
        f.write("{}")
    return base


def main(bench: str, work: str) -> None:
    import jpype
    bm = os.path.basename(os.path.normpath(bench))
    work = os.path.abspath(work)
    for d in ("tmp", "output"):
        os.makedirs(os.path.join(work, d), exist_ok=True)
    pw = open(os.path.join(XD, "secret")).read().strip()
    port = int(os.environ.get("XD_PORT", "55930"))
    z3 = os.environ.get("XD_Z3", os.path.join(XD, "z3w"))
    db = "xd_" + re.sub(r"\W", "_", os.environ.get("XD_DB", os.path.basename(work))).lower()[:50]
    fresh_db(db, port)
    pdir = write_props(work, port, pw, z3, db)
    base = prepare_input(work, bench, bm)
    cp = [pdir] + sorted(os.path.join(JARS, j) for j in os.listdir(JARS) if j.endswith(".jar"))
    os.chdir(os.path.join(work, "tmp"))
    jpype.startJVM(os.environ.get("XD_JVM", os.path.join(XD, "jdk/lib/server/libjvm.so")),
                   "-Xmx" + os.environ.get("XD_XMX", "6g"),
                   "-Djava.library.path=" + os.path.join(XD, "src/xdata/z3"),
                   *os.environ.get("XD_JVM_OPTS", "").split(), classpath=cp)
    rt = jpype.JClass("test.RegressionTests")
    args = jpype.JArray(jpype.JString)([base, "datasets", "", "queries", "mutants", "",
                                        os.path.join(work, "output")])
    rt.main(args)
    jpype.shutdownJVM()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
