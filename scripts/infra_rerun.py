"""Rerun the batches lost to infrastructure failures (2026-10-22 rule, 投稿与实验总览.md「对手矩阵协议」).

A batch whose record has a runner_error that matches matrix_score.is_infra (the shared validator / database server
timed out or refused the connection) is not a miss of the method. After ALL_DONE these batches, and only these, are
rerun with the same method, seed and budget, against the independent validator PostgreSQL (env2.sh,
scripts/validator_pg2.sh). The rerun records go to --out under the same file names as the original run, so that
`matrix_score.py --runs <run>,<out>` uses a rerun record where it exists (records() prefers error-free records).
A batch that fails again for infrastructure reasons stays `infra` (reported as missing, never imputed).

  setsid nohup ../venv-seer/bin/python scripts/infra_rerun.py --runs runs/server/mx --out runs/server/mx_infra \
      > runs/server/mx_infra/rerun.log 2>&1 &          # plan + run (resumable)
  ../venv-seer/bin/python scripts/infra_rerun.py --runs runs/server/mx --out runs/server/mx_infra --summarize

Writes <out>/infra_original.json (the original counts: records, infra records, per method and seed, batch ids)
and, with --summarize, <out>/infra_rerun_summary.json (per batch: original error, rerun outcome).
"""
import argparse
import collections
import glob
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matrix_launch import D, PLAN, PY_EVAL, PY_SEER, ROOT, idle_cores  # noqa: E402
from matrix_score import is_infra  # noqa: E402

NAME = re.compile(r"^(?P<ds>[a-z0-9_]+)\.(?P<m>[a-z0-9_]+)\.s(?P<seed>\d+)(?:\.\d+)?\.jsonl$")


def scan(run):
    """{(ds, method, seed): {batch_id: infra error}} for batches with an infra-failed record and no clean record
    (a batch that also has a clean record, e.g. after a relaunch, is not rerun), and the number of records."""
    clean, infra = collections.defaultdict(set), collections.defaultdict(dict)
    n = 0
    for f in sorted(glob.glob(os.path.join(run, "*.jsonl"))):
        m = NAME.match(os.path.basename(f))
        if not m:
            continue
        key = (m["ds"], m["m"], int(m["seed"]))
        for l in open(f):
            if not l.strip():
                continue
            try:
                r = json.loads(l)
            except ValueError:
                continue
            n += 1
            err = r.get("runner_error")
            if not err:
                clean[key].add(r["batch_id"])
            elif is_infra(err):
                infra[key][r["batch_id"]] = err
    return {k: {b: e for b, e in v.items() if b not in clean[k]} for k, v in infra.items()}, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True, help="original run directory (wave 1)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--env-file", default=f"{ROOT}/env2.sh")
    ap.add_argument("--mem-gb", type=float, default=8)
    ap.add_argument("--max-workers", type=int, default=4, help="workers per (dataset, method, seed) job")
    ap.add_argument("--summarize", action="store_true")
    ap.add_argument("--skip-methods", default="seer,v2",
                    help="methods not rerun here: SEER and v2 are rerun on every batch at the new commit (2026-10-22)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    orig_f = os.path.join(a.out, "infra_original.json")
    if not os.path.exists(orig_f):
        lost, n = scan(a.runs)
        json.dump({"scanned": time.strftime("%F %T"), "run": os.path.abspath(a.runs), "records": n,
                   "infra_records": sum(len(v) for v in lost.values()),
                   "per_job": {f"{d}.{m}.s{s}": {"infra": len(v), "ids": sorted(v),
                                                  "errors": {b: e[:200] for b, e in sorted(v.items())}}
                               for (d, m, s), v in sorted(lost.items()) if v}},
                  open(orig_f, "w"), indent=1)
    orig = json.load(open(orig_f))
    skip = set(x for x in a.skip_methods.split(",") if x)
    if a.summarize:
        per, totals = {}, collections.Counter()
        for job, v in orig["per_job"].items():
            ds, m, s = job.rsplit(".", 2)[0], job.rsplit(".", 2)[1], job.rsplit(".", 2)[2]
            got = {}
            for f in glob.glob(os.path.join(a.out, f"{ds}.{m}.{s}*.jsonl")):
                for l in open(f):
                    if l.strip():
                        r = json.loads(l)
                        e = r.get("runner_error")
                        got[r["batch_id"]] = ("infra_again" if is_infra(e) else "error" if e else "ok")
            miss = "superseded" if m in skip else "not_rerun"
            per[job] = {b: got.get(b, miss) for b in v["ids"]}
            totals.update(per[job].values())
        json.dump({"original_records": orig["records"], "original_infra": orig["infra_records"],
                   "rerun_outcomes": dict(totals), "per_job": per}, open(os.path.join(a.out, "infra_rerun_summary.json"), "w"),
                  indent=1)
        print(json.dumps({"original": f"{orig['infra_records']}/{orig['records']}", "rerun": dict(totals)}))
        return
    env = dict(os.environ, SEER_TOOLS_ROOT=ROOT)
    for l in open(a.env_file):                             # PG / MySQL URLs (never printed)
        l = l.strip()
        if l.startswith("export ") and "=" in l:
            k, v = l[7:].split("=", 1)
            env[k] = v.strip("'\"")
    jobs = []
    for job, v in orig["per_job"].items():
        ds, m, s = job.rsplit(".", 2)
        if m in skip:
            continue
        ids = os.path.join(a.out, f"{job}.ids")
        open(ids, "w").write("\n".join(v["ids"]) + "\n")
        jobs.append((ds, m, int(s[1:]), ids, min(a.max_workers, len(v["ids"]))))
    cores = idle_cores(sum(j[-1] for j in jobs))
    json.dump({"start": time.strftime("%F %T"), "uptime_start": os.popen("uptime").read().strip(),
               "env_file": a.env_file, "cores": cores, "jobs": jobs,
               "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()},
              open(os.path.join(a.out, "rerun_start.json"), "w"), indent=1)
    procs, ci, lim = [], 0, int(a.mem_gb * 2 ** 30)
    for ds, m, seed, ids, w in jobs:
        budget, my = PLAN[ds][0], cores[ci:ci + w]
        ci += w
        data = f"{D}/{ds}.jsonl"
        if m in ("seer", "v2"):
            for i, c in enumerate(my):
                out = os.path.join(a.out, f"{ds}.{m}.s{seed}.{i}.jsonl")
                cmd = ["taskset", "-c", str(c), PY_SEER, "scripts/seer_sqlwitness.py", "--eval-root", ".",
                       "--dataset", data, "--method", m, "--budget", str(budget), "--seed", str(seed),
                       "--shard", f"{i}/{w}", "--mem-gb", str(a.mem_gb), "--only", ids, "--out", out]
                procs.append(subprocess.Popen(cmd, stdout=open(out[:-6] + ".err", "a"), stderr=subprocess.STDOUT,
                                              env=env))
        else:
            out = os.path.join(a.out, f"{ds}.{m}.s{seed}.jsonl")
            cmd = ["prlimit", f"--as={lim}:unlimited", "--", "taskset", "-c", ",".join(map(str, my)), PY_EVAL,
                   "eval/run_baseline.py", "--tool", m, "--dataset", data, "--seed", str(seed), "--budget",
                   str(budget), "--per-call", "60", "--restart", "--workers", str(w), "--only", ids, "--out", out]
            procs.append(subprocess.Popen(cmd, stdout=open(out[:-6] + ".log", "a"), stderr=subprocess.STDOUT,
                                          env=env))
    rc = [p.wait() for p in procs]
    json.dump({"end": time.strftime("%F %T"), "uptime_end": os.popen("uptime").read().strip(), "rc": rc},
              open(os.path.join(a.out, "rerun_end.json"), "w"), indent=1)
    open(os.path.join(a.out, "DONE"), "w").close()


if __name__ == "__main__":
    main()
