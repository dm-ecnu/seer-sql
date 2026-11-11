from seer.front import Front
from seer.representative_search import RepresentativeSearch
from seer2.schema import Schema


def test_nonrecursive_cte_can_shadow_base_table():
    schema = Schema('CREATE TABLE product(product_id INTEGER, product_name TEXT);', 'mysql')
    sql = ('WITH product AS (SELECT product_id, product_name FROM product) '
           'SELECT product_id FROM product')
    relation = Front(schema, 'mysql').translate(sql)
    assert relation.names == ('product_id',)
    search = RepresentativeSearch(schema, 'mysql')
    search.add_query('q', sql)
    assert 'q' in search.irs and 'q' not in search.unsupported


def test_nonrecursive_cte_can_use_earlier_binding():
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')
    sql = 'WITH a AS (SELECT x FROM t), b AS (SELECT x FROM a) SELECT x FROM b'
    relation = Front(schema, 'sqlite').translate(sql)
    assert relation.names == ('x',)


def test_front_recursion_is_recorded_as_unsupported(monkeypatch):
    schema = Schema('CREATE TABLE t(x INTEGER);', 'sqlite')
    search = RepresentativeSearch(schema, 'sqlite')
    def recurse(_):
        raise RecursionError('source nesting')
    monkeypatch.setattr(search.front, 'translate', recurse)
    search.add_query('bad', 'SELECT x FROM t')
    assert search.unsupported == {'bad': 'front_recursion_limit'}
    assert 'bad' not in search.irs
