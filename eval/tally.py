"""Tally run_baseline outputs: per (tool, dataset) file, the number of batches, the tool-call outcomes summed over
batches (genuine / spurious / violating / load_error / exec_error from the validator; eq / timeout / unsupported /
error / no_budget from the tool), #counterexample DBs, classes, budget exhaustion and runner errors.

  python eval/tally.py RUN_DIR/*.jsonl [--reasons]
"""
import argparse, json, os
from collections import Counter

KEYS = ["genuine", "spurious", "violating", "load_error", "exec_error", "eq", "timeout", "unsupported", "error",
        "no_budget", "no_instance"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--reasons", action="store_true", help="top reasons for unsupported/error calls")
    a = ap.parse_args()
    print("file".ljust(36), "B", "q", *[k[:6] for k in KEYS], "dbs", "cls", "exh", "rerr", "wall", sep="\t")
    for f in a.files:
        rows = [json.loads(l) for l in open(f) if l.strip()]
        c, reasons = Counter(), Counter()
        for r in rows:
            c.update(r.get("counts") or {})
            for p in r.get("calls") or []:
                if p.get("verdict") in ("unsupported", "error"):
                    reasons[(p["verdict"], (p.get("reason") or p.get("status") or "")[:90])] += 1
        print(os.path.basename(f)[:-6].ljust(36), len(rows), sum(r["n_queries"] for r in rows),
              *[c.get(k, 0) for k in KEYS], sum(r.get("n_dbs", 0) for r in rows),
              sum(r.get("n_classes", 0) for r in rows), sum(bool(r.get("budget_exhausted")) for r in rows),
              sum(bool(r.get("runner_error")) for r in rows),
              round(sum(r.get("wall_secs", 0) for r in rows) / max(len(rows), 1), 1), sep="\t")
        if a.reasons:
            for (v, why), n in reasons.most_common(4):
                print("    ", n, v, why)


if __name__ == "__main__":
    main()
