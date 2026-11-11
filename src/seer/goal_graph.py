"""Guarded compression of pair-indexed symbolic separation goals.

Only exact, total comparison relations are compressed. The native certificate
relation is not transitively closed. Generic union-find is NetworkX's API.
"""
import collections,functools,hashlib,sys,time
from pathlib import Path
import z3

WHEEL_SHA256='df5d4365b724cf81b8c6a7312509d0c22386097011ad1abe274afd5e9d3bbc5f'

def dependency_path():
    return Path(__file__).resolve().parents[3]/'dependencies/networkx-3.4.2/networkx-3.4.2-py3-none-any.whl'

@functools.lru_cache(None)
def union_find_class():
    path=dependency_path()
    assert hashlib.sha256(path.read_bytes()).hexdigest()==WHEEL_SHA256,'unexpected graph dependency'
    sys.path.insert(0,str(path))
    import networkx
    assert networkx.__version__=='3.4.2'
    assert str(path) in str(networkx.__file__),'graph dependency loaded from an unpinned origin'
    return networkx.utils.UnionFind


def select_pairs(level,pairs,policy='all_pairs'):
    if policy not in ('all_pairs','safe_forest'):raise ValueError('unknown goal policy: '+policy)
    started=time.perf_counter()
    info={'policy':policy,'input_pairs':len(pairs),'selected_pairs':len(pairs),'omitted_pairs':0,
          'eligibility_evaluated':policy=='safe_forest','eligible_pairs':None,'protected_pairs':None,
          'relation_groups':None,'eligible_vertices':None,'eligible_components':None,'forest_edges':None,'protection_reasons':{},'selection_s':0.0,'dependency_sha256':None}
    if policy=='all_pairs':
        info['selection_s']=time.perf_counter()-started
        return pairs,info
    UnionFind=union_find_class();trees={};flags={};selected=[];eligible=0;reasons=collections.Counter()
    def state(q):
        if q not in flags:
            level._tick();out=level.out[q]
            err=level.err.get(q)
            total=err is not None and z3.is_false(z3.simplify(err))
            det=z3.simplify(out.det) if out.det is not None else None
            flags[q]=(total,tuple(out.types),out.pos is None,z3.is_true(det),z3.is_false(det),
                      getattr(level.irs.get(q),'op',None)=='sort')
        return flags[q]
    for index,pair in enumerate(pairs):
        if index%128==0:level._tick()
        if len(pair)!=2 or pair[0]==pair[1]:raise ValueError('goal compression needs pair-indexed edges')
        a,b=pair;A,B=state(a),state(b);reason=None;mode=None
        if not A[0] or not B[0]:reason='runtime_validity'
        elif A[1]!=B[1]:reason='output_types'
        elif A[2] or B[2] or A[4] or B[4]:mode='bag'
        elif A[3] and B[3] and A[5] and B[5]:mode='dense_determinate_list'
        else:reason='order_or_position_contract'
        if reason:
            selected.append(pair);reasons[reason]+=1;continue
        eligible+=1;key=(mode,A[1])
        if key not in trees:trees[key]=UnionFind()
        tree=trees[key]
        if tree[a]!=tree[b]:
            tree.union(a,b);selected.append(pair)
    vertices=sum(sum(1 for _ in tree) for tree in trees.values())
    components=sum(sum(1 for _ in tree.to_sets()) for tree in trees.values())
    forest_edges=len(selected)-sum(reasons.values())
    assert forest_edges==vertices-components
    info.update({'selected_pairs':len(selected),'omitted_pairs':len(pairs)-len(selected),
        'eligible_pairs':eligible,'protected_pairs':sum(reasons.values()),'relation_groups':len(trees),
        'eligible_vertices':vertices,'eligible_components':components,'forest_edges':forest_edges,
        'protection_reasons':dict(reasons),'selection_s':time.perf_counter()-started,
        'dependency_sha256':WHEEL_SHA256})
    assert info['selected_pairs']+info['omitted_pairs']==info['input_pairs']
    return selected,info
