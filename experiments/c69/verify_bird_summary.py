"""Cross-check the emitted BIRD table and every frozen efficiency point."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import csv,json,math,statistics
from pathlib import Path
P=Path((SEER_ROOT + '/repro/c69-production-v1'))
D=json.loads((P/'tables/table-birdpool-c69.json').read_text())
rows=list(csv.DictReader((P/'tables/table-birdpool-c69.csv').open()))
points=list(csv.DictReader((P/'tables/birdpool-c69-size-points.csv').open()))
assert len(rows)==3 and len(points)==400
assert D['expected_units']==23010==D['completed_units']+D['failed_units']
assert D['missing_terminal_units']==0 and D['test_identity_pairs_per_seed']==1243*55
for method,t in D['test_method_totals'].items():
 units=[u for u in D['receipts']if u['method']==method and u['split']=='test']
 assert t['expected_test_units']==6215==len(units)+t['failed_test_units']
 counts=[sum(u['mask'].bit_count()for u in units if u['seed']==s)for s in range(5)]
 assert counts==[x['certified_separated_pairs']for x in t['per_seed']]
 assert counts[0]==t['certified_separated_seed0']
 assert math.isclose(statistics.mean(counts),t['certified_separated_mean'])
 assert math.isclose(statistics.stdev(counts),t['certified_separated_sample_sd'])
for opponent,e in D['paired_efficiency_test_200'].items():
 ps=[r for r in points if r['opponent']==opponent];assert len(ps)==e['expected_sample_batches']==200
 assert len(e['sample_frame'])==200
 for metric in ('wall','request'):
  available=[r for r in ps if r[metric+'_ratio']]
  ratio=math.exp(math.fsum(float(r['design_weight'])*math.log(float(r[metric+'_ratio']))for r in available)/math.fsum(float(r['design_weight'])for r in available))
  actual=e['wall'if metric=='wall'else'requests'];assert len(available)==actual['batches']
  assert math.isclose(ratio,actual['geomean_ratio_opponent_over_seer'],rel_tol=1e-12)
  assert all(math.isfinite(v)and v>0 for v in actual['ci95']) and actual['ci95'][0]<=actual['ci95'][1]
 if e['missing_sample_batches']:
  assert e['full_sample_wall_ratio'] is None
  assert e['seer_faster_full_sample']['expected_batches']==200
  assert len([r for r in ps if int(r['available_seeds'])<5])==e['missing_sample_batches']
 assert sum(b['expected_sample_batches']for b in D['size_bins'][opponent])==200
print('PASS: 3 method rows, 400 frozen points, complete failure denominators, five-seed certification totals, weighted geometric means, CI fields, and size-bin coverage.')
