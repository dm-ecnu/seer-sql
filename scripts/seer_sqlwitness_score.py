"""Score SEER / v2 / Polygon runs on the SQLWitness datasets.

  python scripts/seer_sqlwitness_score.py DATASET --seer 'a*.jsonl' --v2 'b*.jsonl' --polygon c.jsonl [--json OUT]

A pair is (gold, candidate); its key is (problem or batch, candidate qid), so LeetCode batches (SEER, v2) and
LeetCode pairs (Polygon, a sample) line up. There are no equivalence labels: the known-inequivalent set is the union
of the pairs any method split with a certificate the shared validator classifies `genuine` (the instance satisfies
every declared constraint on the dataset's own engine and the two queries differ there). Recall is measured
against that union; a false split is a split whose certificate is not genuine. Polygon's decidable subset is the
set of pairs on which it returned eq or neq.
"""
import argparse
import collections
import glob
import json
import statistics


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def key(r, cand):
    if r["dataset"].startswith("leetcode"):
        prob = (r.get("meta") or {}).get("problem") or r["batch_id"].split("-")[1]
        return (prob, cand)
    return (r["batch_id"], cand)


def load_seer(pats, lc_meta):
    out, walls, crashes = {}, [], collections.Counter()
    recs = {}                       # one record per batch; a re-run without runner error replaces a crashed one
    for p in pats:
        for f in sorted(glob.glob(p)):
            for l in open(f):
                if l.strip():
                    r = json.loads(l)
                    if r["batch_id"] not in recs or (recs[r["batch_id"]].get("runner_error") and not r.get("runner_error")):
                        recs[r["batch_id"]] = r
    for r in recs.values():
        if r.get("runner_error"):
            crashes[r["runner_error"].split(":")[0][:40]] += 1
            for c in lc_meta.get(r["batch_id"], ["cand"]):
                out[key(r, c)] = {"verdict": "crash", "reason": r["runner_error"][:80]}
            continue
        walls.append((r.get("wall", 0), r.get("partition_s")))
        unsup = r.get("unsupported") or {}
        for c, v in (r.get("pairs") or {}).items():
            v = dict(v)
            if c in unsup:
                v["unsup"] = unsup[c]
            if "gold" in unsup:
                v["unsup_ref"] = unsup["gold"]
            out[key(r, c)] = v
    return out, walls, crashes


def load_polygon(pats):
    out, walls = {}, []
    for p in pats:
        for f in sorted(glob.glob(p)):
            for l in open(f):
                r = json.loads(l)
                if r.get("runner_error"):
                    continue
                walls.append(r.get("wall_secs", 0))
                for c in r.get("calls", []):
                    cand = c["b"] if c["a"] == "gold" else c["a"]
                    out[key(r, cand)] = {"verdict": {"neq": "split", "eq": "bounded_equivalent"}.get(c["verdict"],
                                                                                                    c["verdict"]),
                                         "cls": c.get("class"), "reason": c.get("reason") or c.get("status")}
    return out, walls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--seer", nargs="+", default=[])
    ap.add_argument("--v2", nargs="+", default=[])
    ap.add_argument("--polygon", nargs="+", default=[])
    ap.add_argument("--batches", help="dataset jsonl (for LeetCode candidate lists and labels)")
    ap.add_argument("--json")
    a = ap.parse_args()
    lc_meta, lc_label = {}, {}
    if a.batches:
        for l in open(a.batches):
            b = json.loads(l)
            cands = [x["qid"] for x in b["queries"] if x.get("role") != "reference"]
            lc_meta[b["batch_id"]] = cands
            for x in b["queries"]:
                if "leetcode_correct" in x:
                    lc_label[key(b, x["qid"])] = x["leetcode_correct"]
    M = {}
    T = {}
    C = {}
    if a.seer:
        M["seer"], T["seer"], C["seer"] = load_seer(a.seer, lc_meta)
    if a.v2:
        M["v2"], T["v2"], C["v2"] = load_seer(a.v2, lc_meta)
    if a.polygon:
        M["polygon"], T["polygon"] = load_polygon(a.polygon)
    full = [m for m in ("seer", "v2") if m in M]
    common = set.intersection(*(set(M[m]) for m in full)) if full else set()
    known = {k for m in M for k, v in M[m].items() if v.get("verdict") == "split" and v.get("cls") == "genuine"}
    res = {"dataset": a.dataset, "pairs_common": len(common), "known_neq": len(known & common) if common else len(known)}
    print(f"== {a.dataset}: pairs (seer∩v2) {len(common)}, known-neq (validator-genuine by any method) "
          f"{len(known & common)}")
    for m in M:
        dom = common if m in full else set(M[m])
        vs = [M[m][k] for k in dom]
        cnt = collections.Counter(v["verdict"] for v in vs)
        gen = {k for k in dom if M[m][k]["verdict"] == "split" and M[m][k].get("cls") == "genuine"}
        # a certificate the validator itself fails on (a harness error) is unvalidated, neither genuine nor false
        unval = sum(1 for k in dom if M[m][k]["verdict"] == "split" and str(M[m][k].get("cls")).startswith("validator_error"))
        fs = collections.Counter(M[m][k].get("cls") for k in dom if M[m][k]["verdict"] == "split"
                                 and M[m][k].get("cls") != "genuine"
                                 and not str(M[m][k].get("cls")).startswith("validator_error"))
        kn = known & dom
        line = {"n": len(dom), "verdicts": dict(cnt), "genuine": len(gen), "recall": len(gen) / max(1, len(kn)),
                "known_in_scope": len(kn), "false_splits": dict(fs), "unvalidated": unval}
        if m in full:
            ws = [w for w, _ in T[m]]
            ps = [p if p is not None else w for w, p in T[m]]
            line.update({"wall_med": statistics.median(ws) if ws else None, "wall_p90": q(ws, .9),
                         "part_med": statistics.median(ps) if ps else None, "part_p90": q(ps, .9),
                         "crashes": dict(C[m])})
        else:
            line.update({"wall_med": statistics.median(T[m]) if T[m] else None, "wall_p90": q(T[m], .9)})
        res[m] = line
        print(f"  {m:8s} n={len(dom)} genuine {len(gen)}/{len(kn)} recall {line['recall']:.3f}  false splits "
              f"{sum(fs.values())} {dict(fs)}  unvalidated {unval}  wall med {line['wall_med']} p90 {line['wall_p90']}"
              + (f"  partition med {line['part_med']} p90 {line['part_p90']}  crashes {sum(C[m].values())}"
                 if m in full else ""))
        print(f"           verdicts {dict(cnt)}")
    if "polygon" in M:
        D = {k for k, v in M["polygon"].items() if v["verdict"] in ("split", "bounded_equivalent")}
        Dc = D & common if common else D
        kD = known & Dc
        out = {"decidable": len(Dc), "known_neq": len(kD)}
        for m in M:
            g = {k for k in Dc if M[m].get(k, {}).get("verdict") == "split" and M[m][k].get("cls") == "genuine"}
            out[m] = len(g)
        res["polygon_decidable"] = out
        print(f"  Polygon-decidable subset: {len(Dc)} pairs, known-neq {len(kD)}: "
              + ", ".join(f"{m} {out[m]}" for m in M))
        contra = [k for k in Dc if M["polygon"][k]["verdict"] == "bounded_equivalent" and k in known]
        res["polygon_eq_but_genuine_neq"] = len(contra)
        print(f"  Polygon says eq but a genuine counterexample exists: {len(contra)}")
    if lc_label:
        bad = {k for k, v in lc_label.items() if v is False}
        for m in full:
            dom = set(M[m]) & set(lc_label)
            g = {k for k in dom if M[m][k]["verdict"] == "split" and M[m][k].get("cls") == "genuine"}
            sb = {k for k in dom if M[m][k]["verdict"] == "split"}
            res.setdefault("leetcode", {})[m] = {"incorrect": len(bad & dom), "caught": len(g & bad),
                                                "split_correct_labelled": len(sb - bad)}
            print(f"  LeetCode label: {m} catches {len(g & bad)}/{len(bad & dom)} judged-incorrect; splits "
                  f"{len(sb - bad)} judged-correct (validator classes: "
                  f"{dict(collections.Counter(M[m][k].get('cls') for k in sb - bad))})")
    if "seer" in M:
        miss = collections.Counter()
        ex = {}
        for k in (known & common if common else known):
            v = M["seer"].get(k)
            if v is None or (v["verdict"] == "split" and v.get("cls") == "genuine"):
                continue
            why = v["verdict"]
            if v["verdict"] == "split":
                why = f"split, validator {v.get('cls')}"
            elif v.get("unsup") or v.get("unsup_ref"):
                why += f" / unsupported {v.get('unsup') or v.get('unsup_ref')}"
            elif v.get("reason"):
                why += f" / {v['reason'][:60]}"
            elif v.get("k"):
                why += f" / k={v['k']}"
            miss[why] += 1
            ex.setdefault(why, []).append(k)
        res["seer_missed"] = {w: {"n": n, "examples": ex[w][:5]} for w, n in miss.most_common()}
        print("  SEER missed known-neq:", sum(miss.values()))
        for w, n in miss.most_common(15):
            print(f"    {n:4d}  {w}   e.g. {ex[w][:3]}")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=1, default=str)


if __name__ == "__main__":
    main()
