"""Isolated pair-indexed shell around the existing official pair-tool adapters.

Native algorithms stay in tools.call. Cached databases skip a call only when
classify_from_check confirms that exact pair; no signature equality classes.
"""
from __future__ import annotations
import itertools,time
from .shell import _State,_tool_call,BOUNDED,NO_RESTART,CLOSED_STAGES,RESTART_STAGES,VALIDATOR_TIMEOUT_S
from .validator import classify_from_check,empty_db_errors

PAIR_TOOLS={"sqlwitness","parseval","verieql","polygon","sqldriller","sqldriller_official"}


def pairwise_graph_shell(tool,schema,dialect,queries,budget_s,per_call_s=60,seed=0,restart=False):
    if tool not in PAIR_TOOLS:
        raise ValueError("native multi-query tools require their multi-query adapter")
    t0=time.monotonic()
    sql={q["qid"]:q["sql"] for q in queries}
    assert len(sql)==len(queries),"duplicate query id"
    all_pairs=list(itertools.combinations(sql,2))
    errors=empty_db_errors(schema,dialect,sql,timeout_s=VALIDATOR_TIMEOUT_S)
    valid=[q for q in sql if q not in errors]
    state=_State(schema,dialect,sql,valid)
    pending={frozenset(p) for p in itertools.combinations(valid,2)}
    dead=set();calls=[];first_witness={};round_no=0;pair_checks=0
    while pending-dead:
        # No state changes while this list is constructed: compute the round
        # snapshot once, rather than copying the full pending set for every edge.
        eligible=pending-dead
        targets=[p for p in all_pairs if frozenset(p) in eligible]
        if not targets:break
        for a,b in targets:
            key=frozenset((a,b))
            if key not in pending:continue
            left=budget_s-(time.monotonic()-t0)
            if left<1:break
            bound=BOUNDED.get(tool)
            if bound is not None:bound+=round_no
            kwargs={"stages":RESTART_STAGES[tool]} if round_no and tool in RESTART_STAGES else {}
            native=_tool_call(tool,state,[a,b],min(per_call_s,left),seed+1000*round_no,bound=bound,**kwargs)
            record={"a":a,"b":b,"round":round_no,"t":round(time.monotonic()-t0,3),
                    "native":{k:v for k,v in native.items() if k!="instance"}}
            calls.append(record)
            if native["verdict"] in NO_RESTART or native.get("stage") in CLOSED_STAGES:
                dead.add(key)
            if native["verdict"]=="neq":
                instance=native.get("instance")
                if instance is None:
                    record["class"]="no_instance"
                elif time.monotonic()-t0>=budget_s:
                    record.update({"class":"over_budget","instance":instance})
                else:
                    checked=state.run_all(instance)
                    outcome=classify_from_check(checked,a,b)
                    record["comparison"]=outcome
                    if time.monotonic()-t0>=budget_s:
                        record.update({"class":"over_budget","instance":instance})
                    elif outcome["class"]!="genuine":
                        record.update({"class":outcome["class"],"instance":instance})
                    else:
                        idx=len(state.dbs)
                        state.add_db(instance,checked)
                        record.update({"class":"genuine","db":idx})
                        # The target was already validated within budget. Reuse
                        # its exact observation, then visit other pairs in input
                        # order so a deadline never makes set iteration a seed.
                        pending.remove(key)
                        first_witness[key]=idx
                        for x,y in all_pairs:
                            pair=frozenset((x,y))
                            if pair not in pending:continue
                            if time.monotonic()-t0>=budget_s:break
                            pair_checks+=1
                            comparison=classify_from_check(checked,x,y)["class"]
                            if time.monotonic()-t0>=budget_s:break
                            if comparison=="genuine":
                                pending.remove(pair)
                                first_witness[pair]=idx
            if time.monotonic()-t0>=budget_s:break
        if not restart or time.monotonic()-t0>=budget_s:break
        round_no+=1
    evidence={"pairs":[],"n_genuine_pairs":len(first_witness)}
    for a,b in all_pairs:
        key=frozenset((a,b));certificate=first_witness.get(key)
        pair={"a":a,"b":b,"certificate":certificate,
              "verdict":"split" if certificate is not None else "undecided"}
        if a in errors or b in errors:
            pair["reason"]="query_error"
        elif certificate is None:
            pair["reason"]="native_pair_closed" if key in dead else "budget_or_no_witness"
        evidence["pairs"].append(pair)
    return {"state_model":"pair_evidence","pairs":evidence["pairs"],"error_class":sorted(errors),
            "error_msgs":errors,"dbs":state.dbs,"calls":calls,"n_calls":len(calls),
            "n_dbs":len(state.dbs),"pair_comparisons":pair_checks,"n_genuine_pairs":evidence["n_genuine_pairs"],
            "wall_secs":round(time.monotonic()-t0,3),"budget_exhausted":time.monotonic()-t0>=budget_s-1}
