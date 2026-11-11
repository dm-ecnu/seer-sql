import unittest
from score_bird_c69 import pair_inventory,paired_efficiency,METHODS
class BirdScoreTests(unittest.TestCase):
 def test_whitespace_does_not_remove_identity_pairs(self):
  d={'b':{'queries':[{'qid':'a','sql':'SELECT  1'},{'qid':'b','sql':'SELECT 1'},{'qid':'c','sql':'SELECT 1'}]}}
  r=pair_inventory(d,['b'])
  self.assertEqual(r['identity_pairs'],3)
  self.assertEqual(r['raw_unequal_sql_pairs'],2)
  self.assertEqual(r['whitespace_normalized_unequal_sql_pairs'],0)
  self.assertEqual(len(r['whitespace_only_identity_pairs']),2)
 def test_failed_batch_stays_in_full_denominator(self):
  sample=[dict(batch=b,stratum='one',design_weight=w)for b,w in [('ok',1),('failed',3)]]
  units={('ok',seed,method):dict(wall_s=2 if method==METHODS[0]else 6,requests=1 if method==METHODS[0]else 4,censored=False)for seed in range(5)for method in METHODS[:2]}
  r=paired_efficiency(sample,units,METHODS[1])
  self.assertEqual(r['expected_sample_batches'],2);self.assertEqual(r['available_sample_batches'],1)
  self.assertEqual(r['missing_batches'],['failed']);self.assertIsNone(r['full_sample_wall_ratio'])
  self.assertAlmostEqual(r['wall']['geomean_ratio_opponent_over_seer'],3)
  self.assertEqual(r['seer_faster_full_sample']['design_weighted_fraction_bounds'],[.25,1])
  self.assertEqual(len(r['sample_frame']),2)
 def test_empty_bin(self):
  r=paired_efficiency([],{},METHODS[1]);self.assertEqual(r['expected_sample_batches'],0)
  self.assertIsNone(r['wall']);self.assertIsNone(r['seer_faster_full_sample']['point'])
 def test_zero_request_batch_is_explicit_not_dropped(self):
  sample=[dict(batch='b',stratum='one',design_weight=1)]
  units={('b',seed,method):dict(wall_s=2 if method==METHODS[0]else 6,requests=0 if method==METHODS[0]else 4,censored=False)for seed in range(5)for method in METHODS[:2]}
  r=paired_efficiency(sample,units,METHODS[1])
  self.assertEqual(r['expected_sample_batches'],1)
  self.assertEqual(r['zero_request_batches'],['b']);self.assertIsNone(r['requests'])
  self.assertIsNone(r['full_sample_request_ratio']);self.assertAlmostEqual(r['full_sample_wall_ratio'],3)
if __name__=='__main__':unittest.main()
