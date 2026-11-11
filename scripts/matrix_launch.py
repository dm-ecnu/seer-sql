"""Launch one wave of the opponent matrix on eval-server: every (dataset, method) job of the plan for the given
seeds, all at the same time, each worker pinned to its own idle core (taskset), with an address-space cap per
worker; uptime is recorded at the start and the end of the wave. Run it under setsid nohup from the worktree:

  setsid nohup ../venv-seer/bin/python scripts/matrix_launch.py --out runs/server/mx1 --seeds 0 \
      [--datasets tpch,spider_pairs] [--methods seer,sqlwitness] [--smoke] > runs/server/mx1/launch.log 2>&1 &

Budget semantics (identical for every method): B wall-clock seconds per batch, covering everything the method
does on that batch (SEER: front end, refine and fast tiers, symbolic tier, certificate re-checks; baselines: every
tool call including process start, plus the shell's own result signatures); B = min(120 s x candidates, 600 s).
Pairwise tools get at most 60 s per call (their papers' setting) within B, and a call that ends without a genuine
counterexample is repeated (fresh seed; next bound for VeriEQL and Polygon's env.check) until B ends, so that every
method may spend all of B, as SEER does (eval/seereval/shell.py, restart). The shared validator's final check of
SEER/v2 certificates runs after B (for the baselines the shell validates inside B, a few ms per database).
"""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import argparse
import glob
import json
import os
import subprocess
import sys
import time

ROOT = (SEER_ROOT)
D = f"{ROOT}/datasets/batches"
PY_SEER = f"{ROOT}/venv-seer/bin/python"
PY_EVAL = f"{ROOT}/venv-eval/bin/python"

# dataset: (budget s, sample, workers per method, polygon variant)
PLAN = {
    "tpch": (600, 0, 2, "polygon"),
    "tpcds": (600, 0, 8, "polygon"),
    "spider_pairs": (120, 0, 6, "polygon_pair"),
    "bird_pairs": (120, 0, 8, "polygon_pair"),
    "minidev_pg_pairs": (120, 0, 6, "polygon_pair"),
    "leetcode_batches": (600, 0, 4, "polygon"),
    "cubes_d50": (600, 30, 2, "polygon"),
    "cubes_d100": (600, 15, 1, "polygon"),
    # wave 2 (2026-10-22): XDataN's home benchmarks
    "xdatabm": (600, 0, 4, "polygon"),
    "tpch_mut": (600, 0, 2, "polygon"),
}
METHODS = ["seer", "v2", "sqlwitness", "polygon", "parseval", "verieql"]
METHODS2 = ["xdatan", "sqldriller"]       # wave 2: the opponents added 2026-10-22
SMOKE = {"tpch": 2, "tpcds": 2, "spider_pairs": 6, "bird_pairs": 6, "minidev_pg_pairs": 6, "leetcode_batches": 1,
         "cubes_d50": 1, "cubes_d100": 1, "xdatabm": 2, "tpch_mut": 2}


def idle_cores(n, exclude=()):
    def snap():
        out = {}
        for l in open("/proc/stat"):
            if l.startswith("cpu") and l[3].isdigit():
                f = l.split()
                v = list(map(int, f[1:]))
                out[int(f[0][3:])] = (v[3] + v[4], sum(v))
        return out
    a = snap()
    time.sleep(5)
    b = snap()
    busy = {c: 1 - (b[c][0] - a[c][0]) / max(1, b[c][1] - a[c][1]) for c in a}
    cand = sorted((c for c in busy if c not in exclude and c >= 8), key=lambda c: busy[c])
    if len(cand) < n or busy[cand[n - 1]] > 0.3:
        raise SystemExit(f"only {sum(1 for c in cand if busy[c] <= 0.3)} idle cores, need {n}")
    return sorted(cand[:n])


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seeds", default="0")
    ap.add_argument("--datasets", default=",".join(PLAN))
    ap.add_argument("--methods", default=",".join(METHODS))
    ap.add_argument("--budget", type=float, help="override B for every dataset (budget sweep)")
    ap.add_argument("--smoke", action="store_true", help="a few batches per dataset, B = 60 s")
    ap.add_argument("--mem-gb", type=float, default=8)
    ap.add_argument("--workers", type=int, help="override workers per method (timing re-run)")
    ap.add_argument("--sample", type=int, help="override the deterministic sample size (timing re-run)")
    ap.add_argument("--exclude-out", default="", help="other run directories (comma-separated) whose live "
                    "launches' cores are not used (wave 2 next to wave 1)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    jobs = []
    for seed in map(int, a.seeds.split(",")):
        for ds in a.datasets.split(","):
            budget, sample, w, pg = PLAN[ds]
            if a.budget:
                budget = a.budget
            if a.workers:
                w = a.workers
            if a.sample is not None:
                sample = a.sample
            if a.smoke:
                budget, sample, w = 60, SMOKE[ds], 1
            for m in a.methods.split(","):
                # batch datasets: Polygon's native multi-query mode and the pairwise shell around Polygon are both
                # run; the paper reports Polygon in its better mode per dataset (native mode gives up on a whole
                # batch when any one query is outside its fragment)
                for mm in ((["polygon", "polygon_pair"] if pg == "polygon" else [pg]) if m == "polygon" else [m]):
                    jobs.append((ds, mm, seed, budget, sample, w))
    tag = f"s{a.seeds}" + ("" if a.datasets == ",".join(PLAN) else f".{a.datasets.replace(',', '+')}")
    taken = set()                     # cores pinned by unfinished launches (idle between calls)
    for f in [f for d in [a.out] + [x for x in a.exclude_out.split(",") if x]
              for f in glob.glob(os.path.join(d, "wave_start.*.json"))]:
        st = json.load(open(f))
        if not os.path.exists(f.replace("wave_start.", "DONE.")[:-5]) and _alive(st.get("pid")):
            taken.update(st["cores"])
    cores = idle_cores(sum(j[-1] for j in jobs), exclude=taken)
    env = dict(os.environ, SEER_TOOLS_ROOT=ROOT)
    env_file = os.environ.get("SEER_ENV_FILE", f"{ROOT}/env.sh")   # wave 2: env2.sh (own validator PG)
    for l in open(env_file):                              # PG / MySQL URLs (never printed)
        l = l.strip()
        if l.startswith("export ") and "=" in l:
            k, v = l[7:].split("=", 1)
            env[k] = v.strip("'\"")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    meta = {"pid": os.getpid(), "start": time.strftime("%F %T"), "uptime_start": os.popen("uptime").read().strip(), "commit": commit,
            "jobs": [dict(zip(("dataset", "method", "seed", "budget", "sample", "workers"), j)) for j in jobs],
            "n_cores": len(cores), "cores": cores, "mem_gb": a.mem_gb, "env_file": env_file}
    json.dump(meta, open(os.path.join(a.out, f"wave_start.{tag}.json"), "w"), indent=1)
    procs, ci = [], 0
    lim = int(a.mem_gb * 2 ** 30)
    for ds, m, seed, budget, sample, w in jobs:
        data = f"{D}/{ds}.jsonl"
        my = cores[ci:ci + w]
        ci += w
        if m in ("seer", "v2"):
            for i, c in enumerate(my):
                out = os.path.join(a.out, f"{ds}.{m}.s{seed}.{i}.jsonl")
                cmd = ["taskset", "-c", str(c), PY_SEER, "scripts/seer_sqlwitness.py", "--eval-root", ".",
                       "--dataset", data, "--method", m, "--budget", str(budget), "--seed", str(seed),
                       "--shard", f"{i}/{w}", "--mem-gb", str(a.mem_gb), "--out", out]
                if sample:
                    cmd += ["--sample", str(sample)]
                procs.append(subprocess.Popen(cmd, stdout=open(out[:-6] + ".err", "a"), stderr=subprocess.STDOUT,
                                              env=env))
        else:
            out = os.path.join(a.out, f"{ds}.{m}.s{seed}.jsonl")
            # parent soft cap only: each tool child sets its own cap (prlimit --as in seereval/tools.py)
            cmd = ["prlimit", f"--as={lim}:unlimited", "--", "taskset", "-c", ",".join(map(str, my)), PY_EVAL,
                   "eval/run_baseline.py", "--tool", m, "--dataset", data, "--seed", str(seed), "--budget",
                   str(budget), "--per-call", "60", "--restart", "--workers", str(w), "--out", out]
            if sample:
                cmd += ["--sample", str(sample)]
            procs.append(subprocess.Popen(cmd, stdout=open(out[:-6] + ".log", "a"), stderr=subprocess.STDOUT,
                                          env=env))
    print(json.dumps({"launched": len(procs), "cores": len(cores)}), flush=True)
    # job-level CPU time (user + sys of the job and every descendant it reaped), next to the per-batch cpu_secs
    pend, rc, cpu = {p.pid: i for i, p in enumerate(procs)}, [None] * len(procs), [None] * len(procs)
    while pend:
        pid, status, ru = os.wait4(-1, 0)
        if pid in pend:
            i = pend.pop(pid)
            rc[i], cpu[i] = os.waitstatus_to_exitcode(status), round(ru.ru_utime + ru.ru_stime, 1)
    json.dump({"end": time.strftime("%F %T"), "uptime_end": os.popen("uptime").read().strip(), "rc": rc,
               "cpu_secs": cpu},
              open(os.path.join(a.out, f"wave_end.{tag}.json"), "w"), indent=1)
    open(os.path.join(a.out, f"DONE.{tag}"), "w").close()


if __name__ == "__main__":
    main()
