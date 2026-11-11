"""Pair-indexed scheduling preserving the original native multi-query APIs."""
from __future__ import annotations
import itertools,time
from .shell import _State,_tool_call,VALIDATOR_TIMEOUT_S
from .validator import classify_from_check,empty_db_errors


class _Graph:
    def __init__(self,schema,dialect,queries,budget):
        self.started=time.monotonic();self.budget=budget
        self.sql={q["qid"]:q["sql"] for q in queries}
        assert len(self.sql)==len(queries),"duplicate query id"
        self.pairs=list(itertools.combinations(self.sql,2))
        self.errors=empty_db_errors(schema,dialect,self.sql,timeout_s=VALIDATOR_TIMEOUT_S)
        self.valid=[q for q in self.sql if q not in self.errors]
        self.state=_State(schema,dialect,self.sql,self.valid)
        self.pending={frozenset(p) for p in itertools.combinations(self.valid,2)}
        self.witness={};self.calls=[];self.pair_checks=0

    def left(self):return self.budget-(time.monotonic()-self.started)

    def accept(self,instance):
        if self.left()<=0:return {"class":"over_budget","instance":instance}
        checked=self.state.run_all(instance)
        if self.left()<=0:return {"class":"over_budget","instance":instance}
        if checked["status"]!="valid":return {"class":checked["status"],"instance":instance}
        idx=None;gained=[]
        for a,b in self.pairs:
            pair=frozenset((a,b))
            if pair not in self.pending:continue
            if self.left()<=0:break
            self.pair_checks+=1
            outcome=classify_from_check(checked,a,b)["class"]
            if self.left()<=0:break
            if outcome=="genuine":
                if idx is None:
                    idx=len(self.state.dbs);self.state.add_db(instance,checked)
                self.pending.remove(pair);self.witness[pair]=idx;gained.append([a,b])
        if idx is None:
            return {"class":"over_budget" if self.left()<=0 else "no_new_pair_evidence","instance":instance}
        return {"class":"genuine","db":idx,"gained_pairs":gained}

    def result(self):
        pairs=[]
        for a,b in self.pairs:
            cert=self.witness.get(frozenset((a,b)))
            row={"a":a,"b":b,"certificate":cert,"verdict":"split" if cert is not None else "undecided"}
            if a in self.errors or b in self.errors:row["reason"]="query_error"
            pairs.append(row)
        return {"state_model":"pair_evidence","pairs":pairs,"error_class":sorted(self.errors),
                "error_msgs":self.errors,"dbs":self.state.dbs,"n_dbs":len(self.state.dbs),
                "calls":self.calls,"n_calls":len(self.calls),"n_genuine_pairs":len(self.witness),
                "pair_comparisons":self.pair_checks,"wall_secs":round(time.monotonic()-self.started,3),
                "budget_exhausted":self.left()<=1}

    def clique(self,closed):
        # A native disambiguation call must not include an already witnessed
        # pair, which could otherwise return the same irrelevant split forever.
        active=self.pending-closed
        edge=next((p for p in self.pairs if frozenset(p) in active),None)
        if edge is None:return []
        members=list(edge)
        for q in self.valid:
            if q not in members and all(frozenset((q,p)) in active for p in members):
                members.append(q)
        return members


def polygon_graph_multi(schema,dialect,queries,budget_s,per_call_s=60,seed=0):
    graph=_Graph(schema,dialect,queries,budget_s);closed=set()
    while graph.left()>=1:
        members=graph.clique(closed)
        if not members:break
        native=_tool_call("polygon",graph.state,members,min(per_call_s,graph.left()),seed,op="multi")
        record={"members":members,"native":{k:v for k,v in native.items() if k!="instance"},
                "t":round(time.monotonic()-graph.started,3)}
        if native["verdict"]=="neq" and native.get("instance") is not None:
            record.update(graph.accept(native["instance"]))
        gained=record.get("gained_pairs",[])
        requested={frozenset(p) for p in itertools.combinations(members,2)}
        if not any(frozenset(p) in requested for p in gained):
            closed.update(requested)
        graph.calls.append(record)
    result=graph.result();result["native_mode"]="polygon_disambiguate_multi"
    return result


def xdatan_graph_multi(schema,dialect,queries,budget_s,per_call_s=600,seed=0):
    graph=_Graph(schema,dialect,queries,budget_s);tried=set()
    while graph.left()>=1:
        rep=next((q for q in graph.valid if q not in tried and any(q in p for p in graph.pending)),None)
        if rep is None:break
        mutants=[q for q in graph.valid if q!=rep and frozenset((q,rep)) in graph.pending]
        tried.add(rep)
        # This is the original one-query/many-mutants request, not pairwise calls.
        native=_tool_call("xdatan",graph.state,[rep]+mutants,min(per_call_s,graph.left()),seed)
        states=dict(zip(mutants,native.get("states") or []))
        record={"rep":rep,"mutants":mutants,"states":states,
                "claims":[q for q in mutants if states.get(q)=="Killed"],
                "native":{k:v for k,v in native.items() if k!="instances"},"datasets":[]}
        for instance in native.get("instances") or []:
            record["datasets"].append(graph.accept(instance))
        record["backed"]=[q for q in record["claims"] if frozenset((rep,q)) in graph.witness]
        record["t"]=round(time.monotonic()-graph.started,3);graph.calls.append(record)
    result=graph.result();result["native_mode"]="xdatan_one_query_many_mutants"
    return result
