"""Generate ~10 LLM performance rewrites per TPC-H query (LLM-R2 / R-Bot style prompt).
Runs on the Mac (gateway is local). Output: data/rewrites.jsonl, one line per (query, rewrite)."""
import json, os, re, sys, time, concurrent.futures as cf
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "http://127.0.0.1:4000/v1/chat/completions"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "plus"
N = 10
SCHEMA = open(f"{ROOT}/data/tpch_schema.sql").read()
QUERIES = json.load(open(f"{ROOT}/data/tpch_queries.json"))

SYSTEM = ("You are an expert database query optimizer, like the LLM-based rewrite systems LLM-R2 and R-Bot. "
          "Given a schema and a SQL query, you propose query rewrites that return exactly the same result "
          "as the original on the database but may execute faster (e.g. subquery decorrelation/unnesting, "
          "IN/EXISTS to join or semi-join conversion, join elimination, predicate pushdown, aggregate pushdown "
          "or pull-up, redundant DISTINCT/GROUP BY removal, OR-to-UNION, CTE materialization, join reordering, "
          "predicate simplification). You may exploit the declared primary keys, foreign keys and NOT NULL constraints.")

def prompt(q):
    return (f"Schema (PostgreSQL DDL with the TPC-H integrity constraints):\n{SCHEMA}\n\n"
            f"Original query:\n{q}\n\n"
            f"Propose {N} different rewrites of this query that are semantically equivalent (same result rows, "
            "same column order, same ORDER BY/LIMIT behaviour) and potentially faster. Make them diverse: each should "
            "apply a different rewrite strategy or combination. Every rewrite must be a single SELECT statement "
            "valid in both PostgreSQL 16 and DuckDB.\n"
            'Answer with only a JSON object: {"rewrites": [{"strategy": "<short description>", "sql": "<rewritten SQL>"}, ...]}')

def call(qid):
    key = os.environ["LLM_API_KEY"]
    for attempt in range(4):
        try:
            t = time.time()
            r = requests.post(URL, headers={"Authorization": f"Bearer {key}"}, timeout=600, json={
                "model": MODEL, "temperature": 0.7, "max_tokens": 16000,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt(QUERIES[qid])}]})
            r.raise_for_status()
            txt = r.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", txt, re.S)
            rws = json.loads(m.group(0))["rewrites"]
            return qid, [dict(qid=int(qid), rid=i, strategy=w.get("strategy", ""), sql=w["sql"].strip().rstrip(";"),
                              model=MODEL, gen_secs=round(time.time() - t, 1)) for i, w in enumerate(rws)]
        except Exception as e:
            print(f"Q{qid} attempt {attempt}: {type(e).__name__} {str(e)[:200]}", file=sys.stderr)
            time.sleep(5)
    return qid, []

if __name__ == "__main__":
    out = f"{ROOT}/data/rewrites.jsonl"
    done = set()
    if os.path.exists(out):
        done = {json.loads(l)["qid"] for l in open(out)}
    todo = [q for q in QUERIES if int(q) not in done]
    with cf.ThreadPoolExecutor(6) as ex, open(out, "a") as f:
        for qid, rows in ex.map(call, todo):
            for row in rows:
                f.write(json.dumps(row) + "\n")
            f.flush()
            print(f"Q{qid}: {len(rows)} rewrites", flush=True)
