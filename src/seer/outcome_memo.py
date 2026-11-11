"""Reuse the unchanged two-output predicate only for complete identical inputs.

No tolerance-based clustering, transitive closure or cross-database reuse.
A bounded standard-library LRU prevents quadratic cache storage on distinct
outputs. Fingerprints locate buckets; full typed row equality admits reuse.
"""
import datetime as dt
import decimal
from functools import lru_cache
from .core import split_class_n

_SCALARS={type(None),str,bytes,bool,int,float,decimal.Decimal,dt.date,dt.datetime,dt.time}

def _same_rows(left,right):
    if left is None or right is None:return left is right
    if type(left) is not type(right) or len(left)!=len(right):return False
    try:
        for a,b in zip(left,right):
            if type(a) is not type(b) or len(a)!=len(b):return False
            for x,y in zip(a,b):
                if type(x) is not type(y) or type(x) not in _SCALARS or not (x==y):return False
    except (TypeError,ValueError,ArithmeticError):
        return False
    return True

def _bucket(out):
    if type(out.kind) is not str or type(out.ordered) is not bool:
        return None
    if out.msg is not None and type(out.msg) is not str:
        return None
    for index,value in enumerate((out.bag,out.lst)):
        if value is None or type(value) in (str,bytes):
            continue
        if index==0 and type(value) is tuple and len(value)==2 and value[0]=='#n' and type(value[1]) is int:
            continue
        return None
    values=(out.kind,out.bag,out.lst,out.ordered,out.msg)
    key=tuple((type(v),v) for v in values)
    try:hash(key)
    except (TypeError,ValueError,ArithmeticError):return None
    return key

def separated_pairs(pairs,outs,maxsize=4096):
    """Return exactly the original predicate's separated pairs in input order.

    SQL outputs are immutable for the duration of this per-instance operation.
    Unsupported scalar representations are assigned separate outcome groups.
    Query identifiers are renamed in their sorted order for a two-item call;
    no semantic property or equivalence inference follows from group identity.
    """
    assert isinstance(maxsize,int) and maxsize>=0
    groups=[];buckets={};group_of={}
    for qid,out in outs.items():
        key=_bucket(out)
        bucket=buckets.setdefault(key,[]) if key is not None else []
        found=next((g for g in bucket if _same_rows(groups[g].rows,out.rows)),None)
        if found is None:
            found=len(groups);groups.append(out);bucket.append(found)
        group_of[qid]=found
    @lru_cache(maxsize=maxsize)
    def separates(i,j):
        # The original predicate sorts its two query identifiers. Preserve
        # that order explicitly rather than assuming comparison transitivity.
        return len(split_class_n(['0','1'],{'0':groups[i],'1':groups[j]}))==2
    separated=[]
    for pair in pairs:
        a,b=sorted(pair)
        if separates(group_of[a],group_of[b]):separated.append(pair)
    info=separates.cache_info()
    return separated,{'comparison_pairs':info.hits+info.misses,
                      'comparison_evaluations':info.misses,
                      'comparison_cache_hits':info.hits,
                      'comparison_cache_size':info.currsize,
                      'comparison_outcome_groups':len(groups)}
