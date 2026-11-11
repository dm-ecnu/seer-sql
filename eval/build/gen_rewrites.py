"""LLM rewrite candidates for TPC-DS with the same prompt/protocol as the TPC-H set (src/gen_rewrites.py, 2026-10-08):
same system prompt, same user prompt (benchmark name substituted), N=10, model `plus`, temperature 0.7,
max_tokens 16000, up to 4 attempts. Deviations, all recorded per row: (1) the ECNU `plus` upstream stops every
answer at 4,095 completion tokens, so for long queries the 10 rewrites do not fit in one answer; when the answer
is truncated the remaining rewrites are requested in calls asking for 5, then 2, then 1 (`n_per_call`), exact
duplicate SQL dropped; (2) parsing is more tolerant than src/gen_rewrites.py (json strict=False, then json_repair
for complete answers with a syntax slip; `parse`).

Runs where the LLM gateway gateway is reachable: on the Mac (http://127.0.0.1:4000) or on eval-server through the
reverse tunnel (LLM_URL=http://127.0.0.1:4000/v1/chat/completions). The key is read from LLM_API_KEY.

  python eval/build/gen_rewrites.py --bench tpcds --out eval/data/tpcds/rewrites.jsonl [--only 14,23] [--max-tokens N]
"""
import argparse, concurrent.futures as cf, json, os, re, sys, time
import requests

EVAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = os.environ.get("LLM_URL", "http://127.0.0.1:4000/v1/chat/completions")
N = 10
BENCH = {"tpcds": ("TPC-DS", f"{EVAL}/data/tpcds/tpcds_schema.sql", f"{EVAL}/data/tpcds/queries.json"),
         "tpch": ("TPC-H", f"{os.path.dirname(EVAL)}/data/tpch_schema.sql", f"{os.path.dirname(EVAL)}/data/tpch_queries.json")}

SYSTEM = ("You are an expert database query optimizer, like the LLM-based rewrite systems LLM-R2 and R-Bot. "
          "Given a schema and a SQL query, you propose query rewrites that return exactly the same result "
          "as the original on the database but may execute faster (e.g. subquery decorrelation/unnesting, "
          "IN/EXISTS to join or semi-join conversion, join elimination, predicate pushdown, aggregate pushdown "
          "or pull-up, redundant DISTINCT/GROUP BY removal, OR-to-UNION, CTE materialization, join reordering, "
          "predicate simplification). You may exploit the declared primary keys, foreign keys and NOT NULL constraints.")


def prompt(bench_name, schema, q, n=N):
    return (f"Schema (PostgreSQL DDL with the {bench_name} integrity constraints):\n{schema}\n\n"
            f"Original query:\n{q}\n\n"
            f"Propose {n} different rewrites of this query that are semantically equivalent (same result rows, "
            "same column order, same ORDER BY/LIMIT behaviour) and potentially faster. Make them diverse: each should "
            "apply a different rewrite strategy or combination. Every rewrite must be a single SELECT statement "
            "valid in both PostgreSQL 16 and DuckDB.\n"
            'Answer with only a JSON object: {"rewrites": [{"strategy": "<short description>", "sql": "<rewritten SQL>"}, ...]}')


class Truncated(Exception):
    pass


def request(a, bench_name, schema, sql, n):
    """One completion asking for n rewrites; up to 4 attempts for rate limits and JSON slips. Raises Truncated when
    the answer hits the output cap (the ECNU `plus` upstream stops at 4,095 completion tokens whatever max_tokens
    says), so the caller can ask for fewer rewrites per call."""
    key = os.environ["LLM_API_KEY"]
    for attempt in range(4):
        try:
            t = time.time()
            r = requests.post(URL, headers={"Authorization": f"Bearer {key}"}, timeout=900, json={
                "model": a.model, "temperature": 0.7, "max_tokens": a.max_tokens,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": prompt(bench_name, schema, sql, n)}]})
            r.raise_for_status()
            body = r.json()
            ch = body["choices"][0]
            if ch.get("finish_reason") == "length":
                raise Truncated()
            m = re.search(r"\{.*\}", ch["message"]["content"], re.S)
            try:
                rws, parse = json.loads(m.group(0), strict=False)["rewrites"], "json"
            except json.JSONDecodeError:
                import json_repair  # complete answer with a JSON syntax slip, e.g. an unescaped quote inside SQL
                rws, parse = json_repair.loads(m.group(0))["rewrites"], "json_repair"
            usage = body.get("usage") or {}
            return rws, dict(gen_secs=round(time.time() - t, 1), attempt=attempt, parse=parse,
                             completion_tokens=usage.get("completion_tokens"))
        except Truncated:
            raise
        except Exception as e:
            print(f"n={n} attempt {attempt}: {type(e).__name__} {str(e)[:200]}", file=sys.stderr, flush=True)
            time.sleep(60 if "429" in str(e) else 5)
    return [], None


def call(args):
    """10 rewrites per query: one call asking for 10 (the TPC-H protocol); if that answer is truncated, the
    remaining rewrites are requested in calls asking for 5, then 2, then 1 (recorded per row as n_per_call)."""
    qid, sql, a, bench_name, schema = args
    rows, seen = [], set()
    for n in (N, 5, 2, 1):
        calls_left = 2 * ((N - len(rows) + n - 1) // n)
        while len(rows) < N and calls_left > 0:
            calls_left -= 1
            try:
                rws, info = request(a, bench_name, schema, sql, min(n, N - len(rows)) if n < N else N)
            except Truncated:
                print(f"Q{qid}: truncated at n={n}", file=sys.stderr, flush=True)
                break
            if info is None:
                calls_left = 0
                break
            for w in rws:
                s_ = (w.get("sql") or "").strip().rstrip(";")
                if not s_ or s_ in seen or len(rows) >= N:
                    continue
                seen.add(s_)
                rows.append(dict(qid=int(qid), rid=len(rows), strategy=w.get("strategy", ""), sql=s_, model=a.model,
                                 max_tokens=a.max_tokens, n_per_call=n, **info))
        if len(rows) >= N:
            break
    return qid, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="tpcds", choices=list(BENCH))
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="plus")
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    bench_name, schema_p, q_p = BENCH[a.bench]
    schema = open(schema_p).read()
    queries = json.load(open(q_p))
    done = set()
    if os.path.exists(a.out):
        done = {str(json.loads(l)["qid"]) for l in open(a.out)}
    todo = [q for q in queries if q not in done and (not a.only or q in a.only.split(","))]
    print(f"{len(todo)} queries to do", flush=True)
    with cf.ThreadPoolExecutor(a.workers) as ex, open(a.out, "a") as f:
        for qid, rows in ex.map(call, [(q, queries[q], a, bench_name, schema) for q in todo]):
            for row in rows:
                f.write(json.dumps(row) + "\n")
            f.flush()
            print(f"Q{qid}: {len(rows)} rewrites", flush=True)


if __name__ == "__main__":
    main()
