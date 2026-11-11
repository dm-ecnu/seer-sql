import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from c65_summary import paired_ratio,ablation_table,TIMES
from c65_ablation import variant_order,VARIANTS


def test_ratio_groups_seeds_and_preserves_direction_and_zero_missing():
    rows=[dict(problem='a',x=4,y=2)]*5+[dict(problem='b',x=8,y=2)]*5
    r=paired_ratio(rows,'x','y')
    assert r['workloads']==2 and r['paired_units']==10
    assert r['geomean']==pytest.approx(8**.5)
    assert r['ci95']==[2.,4.]
    assert r['numerator_larger_workloads']==2
    r=paired_ratio([dict(problem='a',x=0,y=0),dict(problem='b',x=None,y=2)],'x','y')
    assert r['geomean'] is None and r['zero_pairs']==1


def test_all_six_variants_rotated_without_missing_or_duplicate_units():
    seen=[]
    for batch in range(84):
        for seed in range(5):
            order=variant_order(batch,seed)
            assert len(order)==6 and set(order)==set(VARIANTS)
            seen.extend((batch,seed,variant) for variant in order)
    assert len(seen)==len(set(seen))==2520


def test_ablation_table_keeps_six_rows_and_all_2520_runs():
    rows=[]
    for b in range(84):
        for s in range(5):
            for variant in VARIANTS:
                v=2 if variant=='class_off' else 1
                rows.append(dict(batch=str(b),seed=s,variant=variant,status='complete',censored=False,
                    source_commit='test',objective_scope='',wall_s=float(v),class_block_queries=v,
                    solver_calls=2*v,pair_decision_requests=3,separated_pairs=4,proved_pairs=0,
                    bounded_pairs=2,undecided_pairs=0,class_depth_counts='{}',**dict.fromkeys(TIMES,0.)))
    result=ablation_table(rows)
    assert len(result)==6 and sum(r['observed_runs'] for r in result)==2520
    off=next(r for r in result if r['variant']=='class_off')
    assert off['wall_ratio_vs_full']==off['solver_call_ratio_vs_full']==2
    assert off['class_block_queries_balanced_seed_mean']==168
    assert off['class_block_queries_balanced_seed_sample_sd']==0
    rows.pop()
    with pytest.raises(AssertionError):ablation_table(rows)
