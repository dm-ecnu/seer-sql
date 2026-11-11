"""Run one baseline over a batch dataset under the batch-partition contract (see seereval/shell.py).

  python eval/run_baseline.py --tool sqlwitness --dataset DATA/spider_pairs.jsonl --seed 0 --budget 600 \
      --out OUT.jsonl [--limit 20] [--workers 4] [--per-call 60]

Tools: sqlwitness, parseval, verieql, polygon_pair (pairwise + counterexample cache shell around each), and
polygon (Polygon's own multi-query disambiguation, called repeatedly on unsplit classes).
One output line per batch: partition, error class, #DBs, wall time, per-pair (or per-call) verdicts with the
validator class of every counterexample, counts, and the counterexample databases themselves. Resumes: batches
already in OUT are skipped. Run on eval-server with env.sh sourced (PG/MySQL URLs), not on the Mac.
"""
import argparse, hashlib, json, os, platform, subprocess, sys, time, traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seereval.schema import Schema  # noqa: E402
from seereval import shell, tools  # noqa: E402

TOOLS = ("sqlwitness", "parseval", "verieql", "polygon_pair", "polygon", "xdatan", "sqldriller", "sqldriller_official")


def run_batch(b, a, root):
    t = time.time()
    incomplete_before = tools.incomplete_cpu_calls()
    c_thread, c_tool = time.thread_time(), tools.cpu_used()   # one batch per thread at a time
    q_thread, q_tool = _runq_self(), tools.runq_used()
    try:
        s = Schema.load(os.path.join(root, b["schema_path"]))
        if getattr(a, "comparison_state", "partition") == "pairs":
            from seereval.pair_shell import pairwise_graph_shell
            from seereval.multi_pair_shell import polygon_graph_multi, xdatan_graph_multi
            if a.tool == "xdatan":
                res = xdatan_graph_multi(s, b["dialect"], b["queries"], a.budget, a.budget, a.seed)
            elif a.tool == "polygon":
                res = polygon_graph_multi(s, b["dialect"], b["queries"], a.budget,
                                          a.budget if a.restart else a.per_call, a.seed)
            else:
                tool = "polygon" if a.tool == "polygon_pair" else a.tool
                res = pairwise_graph_shell(tool, s, b["dialect"], b["queries"], a.budget,
                                           a.per_call, a.seed, restart=a.restart)
        elif a.tool == "xdatan":   # XDataN's native one-query-many-mutants mode (seereval/shell.py xdatan_multi)
            res = shell.xdatan_multi(s, b["dialect"], b["queries"], a.budget, a.budget, a.seed)
        elif a.tool == "polygon":
            # with restarts, Polygon's own driver (which deepens its bound until a split or its time ends) gets
            # the whole remaining budget per call instead of the 60 s per-call cap
            res = shell.polygon_multi(s, b["dialect"], b["queries"], a.budget, a.budget if a.restart else a.per_call,
                                      a.seed)
        else:
            tool = "polygon" if a.tool == "polygon_pair" else a.tool
            res = shell.pairwise_shell(tool, s, b["dialect"], b["queries"], a.budget, a.per_call, a.seed,
                                       restart=a.restart)
        err = None
    except Exception as e:
        res, err = {}, f"{type(e).__name__}: {str(e)[:300]} | {traceback.format_exc()[-800:]}"
    if getattr(a, "comparison_state", "partition") == "pairs":
        res.update(state_model="pair_evidence", comparison_contract=a.comparison_contract)
    return {"batch_id": b["batch_id"], "dataset": b["dataset"], "tool": a.tool, "seed": a.seed, "budget": a.budget,
            "per_call": a.per_call, "restart": a.restart, "n_queries": len(b["queries"]), **res, "runner_error": err,
            "runner_wall": round(time.time() - t, 2),
            "cpu_complete": tools.incomplete_cpu_calls() == incomplete_before,
            "incomplete_cpu_calls": tools.incomplete_cpu_calls() - incomplete_before,
            "cpu_secs": round(time.thread_time() - c_thread + tools.cpu_used() - c_tool, 3),
            "runq_secs": round(_runq_self() - q_thread + tools.runq_used() - q_tool, 3)}


def _runq_self():
    """Run-queue delay (s) of the calling thread; 0 where /proc is unavailable."""
    try:
        return int(open("/proc/thread-self/schedstat").read().split()[1]) / 1e9
    except (OSError, ValueError, IndexError):
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", required=True, choices=TOOLS)
    ap.add_argument("--dataset", required=True, help="batch jsonl (schema_path relative to its directory)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--budget", type=float, default=600, help="wall seconds per batch")
    ap.add_argument("--per-call", type=float, default=60, help="seconds per tool call (paper setting: 60)")
    ap.add_argument("--restart", action="store_true",
                    help="repeat a call that found no genuine counterexample (fresh seed / next bound) until the "
                         "batch budget ends")
    ap.add_argument("--comparison-state", choices=["partition", "pairs"], default="partition",
                    help="isolated pair-evidence candidate preserving each native pair/multi-query API")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--sample", type=int, default=0, help="N evenly spaced batches (deterministic) instead of a prefix")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--only", help="file with one batch_id per line (re-runs of a subset, as seer_sqlwitness.py)")
    a = ap.parse_args()
    if a.comparison_state == "pairs":
        base = os.path.dirname(os.path.abspath(__file__))
        paths = [__file__, a.dataset] + [os.path.join(base, "seereval", name) for name in
                               ("pair_shell.py", "multi_pair_shell.py", "shell.py", "tools.py", "validator.py", "schema.py", "engine.py")]
        paths += sorted({os.path.join(os.path.dirname(os.path.abspath(a.dataset)), row["schema_path"])
                         for row in (json.loads(line) for line in open(a.dataset) if line.strip())})
        paths += [str(path) for path in Path(base, "tools").glob("*.py")]
        a.comparison_contract = {"mode":"pair_evidence", "budget_s":a.budget, "per_call_s":a.per_call,
                                 "tool":a.tool, "seed":a.seed, "restart":a.restart,
                                 "files":{os.path.abspath(path):hashlib.sha256(open(path,"rb").read()).hexdigest()
                                          for path in paths}}
    root = os.path.dirname(os.path.abspath(a.dataset))
    batches = [json.loads(l) for l in open(a.dataset)][a.offset:]
    if a.sample and len(batches) > a.sample:
        step = len(batches) / a.sample
        batches = [batches[int(i * step)] for i in range(a.sample)]
    if a.limit:
        batches = batches[:a.limit]
    if a.only:
        keep = {l.strip() for l in open(a.only) if l.strip()}
        batches = [b for b in batches if b["batch_id"] in keep]
    done = set()
    if os.path.exists(a.out):
        previous = [json.loads(l) for l in open(a.out) if l.strip()]
        if a.comparison_state == "pairs":
            assert all(r.get("state_model") == "pair_evidence" and
                       r.get("comparison_contract") == a.comparison_contract for r in previous), \
                "refuse mixed legacy output or changed pair-evidence inputs; retain a separate cohort"
        done = {r["batch_id"] for r in previous}
    todo = [b for b in batches if b["batch_id"] not in done]
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=os.path.dirname(os.path.abspath(__file__)), text=True,
                                         stderr=subprocess.DEVNULL).strip()
    except Exception:
        commit = None
    print(json.dumps({"start": time.strftime("%F %T"), "host": platform.node(), "tool": a.tool, "dataset": a.dataset,
                      "todo": len(todo), "done": len(done), "seed": a.seed, "budget": a.budget,
                      "eval_commit": commit, "uptime": os.popen("uptime").read().strip()}), flush=True)
    with ThreadPoolExecutor(a.workers) as ex, open(a.out, "a") as f:
        for r in ex.map(lambda b: run_batch(b, a, root), todo):
            f.write(json.dumps(r, default=str) + "\n")
            f.flush()
            print(r["batch_id"], r.get("n_classes"), r.get("counts"), r.get("wall_secs"), r["runner_error"] or "",
                  flush=True)
    print(json.dumps({"end": time.strftime("%F %T"), "uptime": os.popen("uptime").read().strip()}), flush=True)


if __name__ == "__main__":
    main()
