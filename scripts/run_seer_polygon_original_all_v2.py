"""Run the published Polygon D-50/D-100 tasks through frozen SEER batches.

Each task receives the published 60-second budget and source query order.
The official Polygon artifact runs separately on adjacent reserved cores.
"""

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
TMPDIR = os.environ.get("TMPDIR", "/tmp")
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import threading
import time


ROOT = Path((SEER_ROOT))
UNIT = ROOT / "seer-m1m8-completeness-wt/scripts/run_seer_polygon_original_unit_v2.sh"
DATA = ROOT / "datasets/batches"
OUT = ROOT / "repro/polygon-original-d50-d100-v1/seer-complete-v2"
STATUS_LOCK = threading.Lock()


def tasks(size):
    path = DATA / f"cubes_d{size}.jsonl"
    with path.open() as source:
        for line in source:
            yield json.loads(line)["batch_id"]


def run_one(size, batch, core):
    group = OUT / f"d{size}"
    output = group / batch
    log = OUT / "logs" / f"d{size}" / f"{batch}.log"
    if (output / "result.json").exists():
        return {"size": size, "batch": batch, "status": "already_complete"}
    if output.exists():
        return {"size": size, "batch": batch, "status": "prior_incomplete"}
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    command = ["taskset", "-c", str(core), "timeout", "--signal=TERM",
               "--kill-after=5s", "150s", "/bin/bash", str(UNIT),
               str(size), batch, str(output)]
    with log.open("w") as sink:
        completed = subprocess.run(command, stdout=sink, stderr=subprocess.STDOUT,
                                   env={**os.environ, "TMPDIR": (TMPDIR)})
    record = {"size": size, "batch": batch, "core": core,
              "exit_code": completed.returncode, "elapsed_s": time.time() - started,
              "status": "failed"}
    result_path = output / "result.json"
    if completed.returncode == 0 and result_path.exists():
        result = json.loads(result_path.read_text())
        metrics = result["metrics"]
        expected = size
        if (metrics["input_queries"] == expected
                and metrics["processed_queries"] + metrics["unprocessed_queries"] == expected
                and metrics["invalid_admitted_certificates"] == 0):
            record["status"] = "complete"
            record["solved"] = result["pair_counts"]["separated"] > 0
            record["method_wall_s"] = metrics["wall_s"]
            record["processed_queries"] = metrics["processed_queries"]
            record["invalid_admitted_certificates"] = 0
        else:
            record["status"] = "invalid_result"
    with STATUS_LOCK:
        with (OUT / "status.jsonl").open("a") as status_file:
            status_file.write(json.dumps(record, separators=(",", ":")) + "\n")
    return record


def run_lane(size, batch_ids, core):
    completed = 0
    failed = 0
    for batch in batch_ids:
        record = run_one(size, batch, core)
        completed += 1
        failed += record["status"] not in ("complete", "already_complete")
        if record["status"] not in ("complete", "already_complete") or completed % 50 == 0:
            print(json.dumps({"lane_core": core, "lane_finished": completed,
                              "lane_total": len(batch_ids), **record}), flush=True)
    return {"core": core, "finished": completed, "failed": failed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cores", required=True, help="comma-separated exclusive physical CPUs")
    args = parser.parse_args()
    cores = [int(c) for c in args.cores.split(",")]
    if len(cores) != len(set(cores)) or not cores:
        raise ValueError("cores must be unique")
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (50, 100):
        batch_ids = list(tasks(size))
        if len(batch_ids) != {50: 4245, 100: 2475}[size]:
            raise RuntimeError("original D task count changed")
        print(json.dumps({"start_size": size, "tasks": len(batch_ids), "cores": cores}), flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(cores)) as pool:
            pending = [pool.submit(run_lane, size, batch_ids[i::len(cores)], core)
                       for i, core in enumerate(cores)]
            for future in concurrent.futures.as_completed(pending):
                print(json.dumps({"lane_done": future.result()}), flush=True)
        print(json.dumps({"finished_size": size, "tasks": len(batch_ids)}), flush=True)


if __name__ == "__main__":
    main()
