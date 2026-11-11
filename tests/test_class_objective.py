import importlib.util
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
import z3
from seer.class_objective import extend_model
from seer.class_search import ClassContext, ClassDepth
from seer.representative_search import RepresentativeSearch
from seer2.schema import Schema
from seer.core import _jsonable


def test_extended_model_preserves_original_block(monkeypatch):
    original_check=ClassDepth.check
    def choose_initial_model(level,goal):
        if level.checks==0:
            assert set(level.out)=={'a','b'}, 'representatives encoded before block SAT'
            # Make the initial representative c equal d; the objective must
            # execute a genuine extension, independent of Z3 model choices.
            goal=z3.And(goal,*[z3.Not(exists) for exists,_ in level.enc.vars['u']])
        return original_check(level,goal)
    monkeypatch.setattr(ClassDepth,'check',choose_initial_model)
    s=RepresentativeSearch(Schema('CREATE TABLE t(x INTEGER); CREATE TABLE u(y INTEGER);','sqlite'),'sqlite')
    for q,sql in {'a':'SELECT COUNT(*) FROM t','b':'SELECT COUNT(x) FROM t',
                  'c':'SELECT COUNT(*) FROM (SELECT y FROM u ORDER BY y LIMIT 1) AS v','d':'SELECT 0'}.items():
        s.add_query(q,sql)
    context=ClassContext(s,'a')
    receipt=context.check(['b'],2,10,[('a','c'),('c','d')])
    assert receipt['status']=='candidate' and 'b' in receipt['differing']
    assert receipt['objective_rounds']>0 and receipt['objective_extra_separations']>0
    assert not s.schema.violations(receipt['database'])
    assert context.solver.num_scopes()==0
    assert receipt['solver_calls']==1+receipt['objective_rounds']
    # Independent execution verifies the retained original separation.
    from seer.stream import StreamPartition
    from seer2.backends import make_backend
    from seer2.refine import Prepared
    replay=SimpleNamespace(backend=make_backend('sqlite',s.schema,timeout=5),
        prepared={q:Prepared(sql,'sqlite','sqlite') for q,sql in
        [('a','SELECT COUNT(*) FROM t'),('b','SELECT COUNT(x) FROM t')]},
        search=SimpleNamespace(irs={}),_count=lambda:None)
    observed=StreamPartition._run_impl(replay,receipt['database'],['a','b'])
    assert observed['a'].stable and observed['b'].stable
    assert observed['a'].key!=observed['b'].key


def test_K_monotone_and_last_model_survives_failure():
    a,b,c=z3.Bools('objective_test_a objective_test_b objective_test_c')
    initial=z3.Solver();initial.add(a,z3.Not(b),z3.Not(c));assert initial.check()==z3.sat
    model=initial.model()
    class Level:
        checks=0;deadline=float('inf');last_unknown_reason=None
        def __init__(self,stop):self.solver=z3.Solver();self.stop=stop
        def check(self,goal):
            self.checks+=1;self.solver.push()
            try:
                if self.checks==2:
                    self.last_unknown_reason='timeout' if self.stop==z3.unknown else None
                    return self.stop,None
                # One new separation at a time so the second round is reached.
                self.solver.add(goal,z3.Not(z3.And(b,c)))
                answer=self.solver.check()
                return answer,self.solver.model() if answer==z3.sat else None
            finally:self.solver.pop()
    for stop in (z3.unsat,z3.unknown):
        level=Level(stop)
        best,r=extend_model(level,model,{'member':a},{('r0','r1'):b,('r1','r2'):c})
        history=[set(map(tuple,k)) for k in r['objective_k_history']]
        assert len(history)==2 and history[0]<history[1]
        assert z3.is_true(best.eval(a))
        assert sum(z3.is_true(best.eval(t)) for t in (b,c))==1
        assert r['objective_rounds']==2 and r['objective_extra_separations']==1
        assert level.solver.num_scopes()==0


def test_objective_off_is_byte_identical_to_25fe74e(monkeypatch):
    root=Path(__file__).resolve().parents[1]
    old=subprocess.check_output(['git','show','25fe74e117387693195d5ffb5ca6995eed7eacab:src/seer/class_search.py'],cwd=root,text=True)
    module=importlib.util.module_from_spec(importlib.util.spec_from_loader('seer.c69_original_class',loader=None))
    exec(compile(old,'25fe74e:class_search.py','exec'),module.__dict__)
    monkeypatch.setattr(time,'perf_counter',lambda:100.)
    for queries in ({'a':'SELECT 1','b':'SELECT 2'},
                    {'a':'SELECT x FROM t','b':'SELECT x FROM t WHERE TRUE'}):
        outputs=[]
        for cls in (module.ClassContext,ClassContext):
            s=RepresentativeSearch(Schema('CREATE TABLE t(x INTEGER);','sqlite'),'sqlite')
            for q,sql in queries.items():s.add_query(q,sql)
            context=cls(s,'a')
            receipts=[context.check(['b'],k,10) for k in (1,2)]
            for receipt in receipts:
                if 'database' in receipt:
                    receipt['database']=_jsonable(receipt['database'])
            outputs.append(json.dumps(receipts,sort_keys=True).encode())
        assert outputs[0]==outputs[1]
