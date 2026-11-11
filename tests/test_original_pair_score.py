from scripts.score_leetcode_verieql_original_v1 import score_batch


def test_gold_pair_score_ignores_candidate_candidate_pairs_and_uses_certified_paths():
    batch = {'meta': {'problem': 'p', 'original_pair_count': 3},
             'queries': [{'qid': 'gold', 'role': 'reference'},
                         *[{'qid': f'c{i}', 'role': 'candidate', 'original_query_id': str(i)}
                           for i in range(3)]]}
    config = {'query_ids': ['gold', 'c0', 'c1', 'c2'],
              'original_pair_timeout_s': 600., 'per_call_s': 600.,
              'budget_s': 1800.}
    result = {'contract': 'canonical-6sig-list-bag-errors-v1',
              'domain_identity': 'scope-a',
              'classes': [['c0', 'gold', 'c1'], ['c2']],
              'unprocessed_query_ids': [],
              'relations': {'gold': {'status': 'bounded'},
                            'c0': {'status': 'representative'},
                            'c1': {'status': 'bounded'},
                            'c2': {'status': 'representative'}},
              'evidence_edges': [{'a': 'gold', 'b': 'c0', 'status': 'proved'},
                                 {'a': 'c0', 'b': 'c1', 'status': 'bounded',
                                  'k': 2, 'domain': 'scope-a'}],
              'metrics': {'input_queries': 4, 'processed_queries': 4,
                          'unprocessed_queries': 0,
                          'invalid_admitted_certificates': 0}}
    rows, counts = score_batch(batch, result, config, 600.)
    assert [r['status'] for r in rows] == ['proved', 'bounded', 'refuted']
    assert rows[1]['bounded_k'] == 2
    assert dict(counts) == {'proved': 1, 'bounded': 1, 'refuted': 1}
