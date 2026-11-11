"""Mechanism checks: performance memo removal and full-Sigma acceptance."""
import sys
from pathlib import Path
import pytest
import z3
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seer.pair_state import PairSeer
from seer.symbolic import Level
from seer.encode import Domain, schema_strings
from seer2.schema import Schema


def engine(ddl, queries, **kw):
    return PairSeer(Schema(ddl, 'sqlite'), queries, dialect='sqlite', backend='sqlite',
                    fast=False, levels=(2,), max_k=2, budget=10, solver_timeout_ms=2000, **kw)


def level(s, **kw):
    return Level(s.S, s.front, Domain.build(list(s.ir.values()), checks_strings=schema_strings(s.S)),
                 s.ir, 2, 'sqlite', timeout_ms=2000, **kw)


def test_deterministic_reencoding_is_active_and_preserves_identical_query_semantics():
    sql = 'SELECT x + 1 FROM t WHERE x > 0'
    s = engine('CREATE TABLE t(x INTEGER NOT NULL);', [('a',sql),('b',sql)])
    shared, separate = level(s), level(s, share_query_encoding=False)
    for lv in (shared, separate):
        assert lv.check(z3.And(lv.valid(['a','b']), lv.sep('a','b')))[0] == z3.unsat
    assert separate.enc.stats['memo_miss'] > shared.enc.stats['memo_miss']
    assert separate.enc.stats['cross_query_memo_entries_discarded'] > 0
    assert separate.enc.stats['query_memo_resets'] == 2
    assert separate.enc.stats['semantic_memo_entries_peak'] == 0


@pytest.mark.parametrize('sql', [
    'SELECT x FROM t LIMIT 1',
    'SELECT x FROM t ORDER BY x LIMIT 1 OFFSET 1',
    'SELECT v.x FROM (SELECT x + 1 AS x FROM t) AS v WHERE EXISTS '
    '(SELECT x FROM t AS u WHERE u.x < v.x LIMIT 1)',
])
def test_identical_limited_queries_keep_choice_and_correlated_input_identity(sql):
    s = engine('CREATE TABLE t(x INTEGER NOT NULL);', [('a',sql),('b',sql)])
    lv = level(s, share_query_encoding=False)
    assert set(lv.out) == {'a','b'}, lv.unsup
    assert lv.enc.stats['semantic_memo_entries_peak'] > 0
    assert lv.check(z3.And(lv.valid(['a','b']), lv.sep('a','b')))[0] == z3.unsat


@pytest.mark.parametrize('ddl, db', [
    ('CREATE TABLE t(x INTEGER NOT NULL);', {'t':[{'x':None}]}),
    ('CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);', {'t':[{'x':1},{'x':1}]}),
    ('CREATE TABLE t(x INTEGER UNIQUE);', {'t':[{'x':1},{'x':1}]}),
    ('CREATE TABLE t(x INTEGER CHECK(x > 0));', {'t':[{'x':-1}]}),
    ('CREATE TABLE p(x INTEGER PRIMARY KEY NOT NULL); CREATE TABLE t(x INTEGER REFERENCES p(x));',
     {'p':[], 't':[{'x':1}]}),
])
def test_sigma_switch_really_removes_each_constraint_but_full_checker_rejects(ddl, db):
    s = engine(ddl, [('a','SELECT x FROM t'),('b','SELECT x FROM t')])
    assert s.S.violations(db)
    for enabled in (True, False):
        lv = level(s, encode_sigma=enabled)
        for table, rows in lv.enc.vars.items():
            data = db.get(table, [])
            names = s.S.tables[table].colnames
            for i, (exists, cells) in enumerate(rows):
                lv.solver.add(exists == (i < len(data)))
                if i < len(data):
                    for name, (value, null) in zip(names, cells):
                        native = data[i][name]
                        lv.solver.add(null == (native is None))
                        if native is not None:
                            lv.solver.add(value == native)
        status = lv.solver.check()
        assert status == (z3.unsat if enabled else z3.sat)
        if not enabled:
            assert s.S.violations(lv.decode(lv.solver.model(), s.S))


def test_no_sigma_never_sends_illegal_symbolic_models_to_database_or_credits_them():
    s = engine('CREATE TABLE t(x INTEGER NOT NULL);',
               [('a','SELECT COUNT(*) FROM t'),('b','SELECT COUNT(x) FROM t')], encode_sigma=False)
    original = s.run_db
    observed = []
    def accepted(db, origin):
        assert not s.S.violations(db), (origin, db)
        observed.append(db)
        return original(db, origin)
    s.run_db = accepted
    result = s.run()
    assert result['stats'].get('symbolic_sigma_rejected',0) > 0, result
    assert result['rejected_symbolic_instances']
    for rejected in result['rejected_symbolic_instances']:
        assert s.S.violations(rejected['db'])
    assert result['certificates']['dbs'] == []
    assert s.verdict('a','b')['verdict'] == 'undecided'
    assert result['encoding_configuration'] == {'share_query_encoding':True,'encode_sigma':False}


def test_hard_runner_rejects_no_op_or_foreign_method_ablation():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    from hard_deadline import ablation_config
    assert ablation_config('pair_seer','no_hard_sigma')['encode_sigma'] is False
    assert ablation_config('pair_seer','no_deterministic_query_memo')['share_query_encoding'] is False
    with pytest.raises(ValueError):
        ablation_config('sqlwitness','no_hard_sigma')
    with pytest.raises(ValueError):
        ablation_config('pair_seer','no_representatives')
