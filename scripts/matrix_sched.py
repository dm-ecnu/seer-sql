"""Server-side service that runs the whole SEER matrix on eval-server without an agent in the loop.

Queue (in order): the main matrix as (seed, dataset) units for seeds 0..4, the LeetCode budget sweep, then one
timing-only re-run of a representative subset with few workers, which starts only in a quiet night window. Every
method of one unit starts at the same time on its own idle cores (scripts/matrix_launch.py), so all methods of a
dataset share a time slot; a unit starts as soon as the machine has enough idle cores (smaller units backfill).

State lives only in the output directories, so the service is resumable: a unit with a DONE marker is skipped; a
unit whose launcher is alive is waited for; a unit that started but whose launcher died is relaunched (its runners
skip the batches already written; the stale wave_start file is kept as wave_start.*.crashed-<time>.json). When a
unit finishes, its dataset is re-scored automatically (test and dev splits) into <out>/scores/. When every unit is
done the service writes <mx>/ALL_DONE and exits.

cron (every 10 min; flock keeps one instance, so a crashed or killed service is restarted):
  */10 * * * * cd $SEER_ROOT/seer-mx-wt && flock -n $SEER_ROOT/sched.lock \
      ../venv-seer/bin/python scripts/matrix_sched.py >> runs/server/mx/sched.log 2>&1
wave 2 (own worktree, out dir and lock):
  */10 * * * * cd $SEER_ROOT/seer-mx2-wt && flock -n $SEER_ROOT/sched2.lock \
      ../venv-seer/bin/python scripts/matrix_sched.py --out runs/server/mx2 \
      --wave2 $SEER_ROOT/seer-mx-wt/runs/server/mx >> runs/server/mx2/sched.log 2>&1
  (cron sets SEER_ENV_FILE=$SEER_ROOT/env2.sh: wave 2 validates on its own PostgreSQL, scripts/validator_pg2.sh)
  Wave 2 also owns timing units for XDataN and SQLDriller (runs/server/timing in its worktree): same subsets and
  workers as the wave-1 timing re-run, started as soon as a wave-1 timing unit has started, so both share one slot;
  no new wave-2 matrix unit starts while any timing unit of either wave is alive.
SEER/v2 rerun at the Σ-constant pool commit (author's approval 2026-10-22), every dataset, seeds 0-4, the sweep and
the timing units, on the wave-2 validator PG, own out dirs and lock:
  */10 * * * * cd $SEER_ROOT/seer-mx2-wt && SEER_ENV_FILE=$SEER_ROOT/env2.sh \
      flock -n $SEER_ROOT/sched3.lock ../venv-seer/bin/python scripts/matrix_sched.py \
      --out runs/server/mx_seer --sweep-out runs/server/sweep_seer --timing-out runs/server/timing_seer \
      --wave2 $SEER_ROOT/seer-mx-wt/runs/server/mx --methods seer,v2 \
      --exclude-out runs/server/mx2,runs/server/timing >> runs/server/mx_seer/sched.log 2>&1
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matrix_launch import D, PLAN, METHODS, METHODS2  # noqa: E402

ORDER = ["bird_pairs", "spider_pairs", "minidev_pg_pairs", "tpcds", "leetcode_batches", "cubes_d50", "tpch",
         "cubes_d100"]
SWEEP = [60, 150, 300, 1200]
# timing-only re-run: (dataset, workers per method, sample); seed 0, B as in the plan
TIMING = [("tpch", 2, 0), ("spider_pairs", 1, 60), ("leetcode_batches", 1, 6)]
QUIET_HOURS = range(0, 6)          # local hours in which the timing re-run may start
SCORE_ARGS = {"tpch": ["--labels", "runs/labels.jsonl"]}

# wave 2 (protocol 2026-10-22): XDataN and SQLDriller on every dataset; on XDataN's two home benchmarks also SEER,
# v2, SQLWitness and ParSEval (Polygon and VeriEQL are reference rows and are not run there). Own out dir and cron;
# wave-1 units and files are never touched. Its scores read waves 1 and 2 together (one known-inequivalent union).
NEW = ["xdatabm", "tpch_mut"]
# xdatabm last: SEER (frozen at d3c3696) has no pool value for columns whose single-column CHECK excludes every
# query constant (section.semester IN (...), section.year > 1701); the decision on it is pending, 2026-10-22
ORDER2 = ["tpch_mut"] + ORDER + ["xdatabm"]
# SEER and v2 are not run by wave 2: since the author's approval of 2026-10-22 they are rerun on every dataset at
# the commit with the Σ-constant value pools (9d5d194) by a third service (--methods seer,v2, below)
METHODS_NEW = ["sqlwitness", "parseval"] + METHODS2
RERUN = ["seer", "v2"]


def need(ds, w=None, methods=None):
    w = w or PLAN[ds][2]
    methods = methods or METHODS
    return w * (len(methods) + (PLAN[ds][3] == "polygon" and "polygon" in methods))


def _session_alive(sid):
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                f = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(f[3]) == sid and f[0] != "Z":
                return True
    return False


def log(msg):
    print(f"{time.strftime('%F %T')} {msg}", flush=True)


class Unit:
    def __init__(self, out, seed, ds, budget=None, workers=None, sample=None, methods=None, exclude_out="",
                 score_runs=None):
        self.out, self.seed, self.ds, self.budget, self.workers, self.sample = out, seed, ds, budget, workers, sample
        self.methods, self.exclude_out, self.score_runs = methods, exclude_out, score_runs or [out]
        self.tag = f"s{seed}.{ds}"
        self.key = f"{out}:{self.tag}"

    def path(self, what):
        return os.path.join(self.out, f"{what}.{self.tag}" + (".json" if what.startswith("wave") else ""))

    def done(self):
        return os.path.exists(self.path("DONE"))

    def alive(self):
        f = self.path("wave_start")
        if not os.path.exists(f):
            return False
        try:
            sid = int(json.load(open(f))["pid"])
        except (OSError, KeyError, ValueError):
            return False
        return _session_alive(sid)        # the launcher or any runner it started (they share its session)

    def launch(self):
        f = self.path("wave_start")
        if os.path.exists(f) and not self.alive():
            os.rename(f, f[:-5] + time.strftime(".crashed-%Y%m%d-%H%M%S.json"))
            log(f"relaunch after crash: {self.key}")
        os.makedirs(self.out, exist_ok=True)
        cmd = [sys.executable, "scripts/matrix_launch.py", "--out", self.out, "--seeds", self.seed,
               "--datasets", self.ds]
        if self.budget:
            cmd += ["--budget", str(self.budget)]
        if self.workers:
            cmd += ["--workers", str(self.workers)]
        if self.sample is not None:
            cmd += ["--sample", str(self.sample)]
        if self.methods:
            cmd += ["--methods", ",".join(self.methods)]
        if self.exclude_out:
            cmd += ["--exclude-out", self.exclude_out]
        p = subprocess.Popen(cmd, stdout=open(os.path.join(self.out, f"launch.{self.tag}.log"), "a"),
                             stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(10)                                   # idle_cores samples for 5 s
        return p.poll() in (None, 0)


def score(u):
    """Re-score u's dataset over every seed present in u.out (test and dev splits)."""
    os.makedirs(os.path.join(u.out, "scores"), exist_ok=True)
    sample = u.sample if u.sample is not None else PLAN[u.ds][1]
    for split in ("test", "dev"):
        base = os.path.join(u.out, "scores", f"{u.ds}.{split}")
        cmd = [sys.executable, "scripts/matrix_score.py", "--dataset", f"{D}/{u.ds}.jsonl", "--runs",
               ",".join(u.score_runs),
               "--split", split, "--json", base + ".json"] + (["--sample", str(sample)] if sample else []) \
            + SCORE_ARGS.get(u.ds, [])
        r = subprocess.run(cmd, capture_output=True, text=True)
        open(base + ".txt", "w").write(r.stdout + (r.stderr[-2000:] if r.returncode else ""))
    log(f"scored {u.key}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="runs/server/mx")
    ap.add_argument("--sweep-out", default="runs/server/sweep")
    ap.add_argument("--timing-out", default="runs/server/timing")
    ap.add_argument("--seeds", default="0,1,2,3,4")
    ap.add_argument("--poll", type=float, default=120)
    ap.add_argument("--wave2", help="wave-1 run directory: run wave 2 into --out instead of the main matrix")
    ap.add_argument("--methods", help="with --wave2: run only these methods on every dataset (main units, budget "
                                      "sweep, timing), e.g. the SEER/v2 rerun at the new commit")
    ap.add_argument("--exclude-out", default="", help="other live run directories whose pinned cores are kept free")
    ap.add_argument("--timing-after", default="", help="files (comma-separated) that must all exist before a timing "
                                                       "unit starts, e.g. the infra rerun's DONE marker")
    ap.add_argument("--timing-methods", help="main matrix: methods of the timing units (default: all)")
    a = ap.parse_args()
    w1_units = []
    if a.wave2:
        w1_timing = os.path.join(os.path.dirname(os.path.abspath(a.wave2)), "timing")
        excl = ",".join(x for x in [a.wave2, w1_timing, a.exclude_out] if x)
        if a.methods:
            ms = a.methods.split(",")
            main_units = [Unit(a.out, s, ds, methods=ms, exclude_out=excl)
                          for s in a.seeds.split(",") for ds in ORDER2]
            main_units += [Unit(f"{a.sweep_out}/B{b}", "0", "leetcode_batches", budget=b, methods=ms,
                                exclude_out=excl) for b in SWEEP]
        else:
            ms = METHODS2
            main_units = [Unit(a.out, s, ds, methods=METHODS_NEW if ds in NEW else METHODS2, exclude_out=excl,
                               score_runs=[a.out] + ([] if ds in NEW else [a.wave2]))
                          for s in a.seeds.split(",") for ds in ORDER2]
        # timing units (approved 2026-10-22): the same subsets, workers and protocol as the wave-1 timing re-run,
        # launched in the same slot as it (as soon as its first unit has started)
        timing = [Unit(a.timing_out, "0", ds, workers=w, sample=n, methods=ms, exclude_out=excl)
                  for ds, w, n in TIMING]
        w1_units = [Unit(w1_timing, "0", ds, workers=w, sample=n) for ds, w, n in TIMING]
    else:
        main_units = [Unit(a.out, s, ds) for s in a.seeds.split(",") for ds in ORDER]
        main_units += [Unit(f"{a.sweep_out}/B{b}", "0", "leetcode_batches", budget=b) for b in SWEEP]
        timing = [Unit(a.timing_out, "0", ds, workers=w, sample=n,
                       methods=a.timing_methods.split(",") if a.timing_methods else None) for ds, w, n in TIMING]
    units = main_units + timing
    gates = [x for x in a.timing_after.split(",") if x]

    def gate_open():                                     # timing only after e.g. the infra rerun has finished
        return all(os.path.exists(g) for g in gates)
    os.makedirs(a.out, exist_ok=True)
    mark = os.path.join(a.out, "scored.json")         # DONE-marker mtime each unit was last scored at
    try:
        scored = json.load(open(mark))
    except (OSError, ValueError):
        scored = {}
    log(f"service start: {sum(u.done() for u in units)}/{len(units)} units done")
    while True:
        for u in units:                                    # score each finished unit once per DONE marker
            if u.done() and scored.get(u.key) != os.path.getmtime(u.path("DONE")):
                score(u)
                scored[u.key] = os.path.getmtime(u.path("DONE"))
                json.dump(scored, open(mark, "w"), indent=1)
        if a.wave2:
            wait = [u for u in timing if not u.done() and not u.alive()]
            w1_started = any(os.path.exists(u.path("wave_start")) for u in w1_units)
            w1_alive = any(u.alive() for u in w1_units)
            late = w1_started and not w1_alive and time.localtime().tm_hour in QUIET_HOURS \
                and not any(u.alive() for u in timing)
            if wait and w1_started and (w1_alive or late) and gate_open():
                ok = [u.launch() for u in wait]
                log(("timing launch with wave-1 slot " if w1_alive else "timing launch, late quiet slot ")
                    + ", ".join(f"{u.tag}={k}" for u, k in zip(wait, ok)))
        timing_busy = any(u.alive() for u in timing + w1_units)
        failed = 10 ** 9
        for u in main_units:
            if timing_busy:                                # keep the timing slot as quiet as wave 2 can
                break
            if u.done() or u.alive():
                continue
            n = need(u.ds, u.workers, u.methods)
            if n >= failed:
                continue
            if u.launch():
                log(f"started {u.key}" + (f" B={u.budget}" if u.budget else ""))
                time.sleep(20)                             # let the new workers show up as busy
            else:
                failed = n
        if a.wave2:
            if all(u.done() for u in units):
                open(os.path.join(a.out, "ALL_DONE"), "w").close()
                log("all units done")
                return
        elif all(u.done() for u in main_units):
            wait = [u for u in timing if not u.done() and not u.alive()]
            if wait and time.localtime().tm_hour in QUIET_HOURS and not any(u.alive() for u in timing) \
                    and gate_open():
                ok = [u.launch() for u in wait]            # all timing units in one slot
                log("timing re-run launch " + ", ".join(f"{u.tag}={k}" for u, k in zip(wait, ok)))
            if all(u.done() for u in units):
                open(os.path.join(a.out, "ALL_DONE"), "w").close()
                log("all units done")
                return
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
