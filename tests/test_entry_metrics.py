import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'eval'),str(ROOT/'src'),str(ROOT/'eval/tools')]
from hard_deadline import Collector,setup_worker,supervise
from entry_metrics import read_entries
from seereval.schema import from_ddl
from pair_matrix_score import score_record


def measured_worker(batch,root,method,budget,seed,deadline,conn,journal,mem_gb):
    setup_worker(conn,mem_gb)
    from entry_metrics import install_host
    install_host(str(Path(journal)/'entries.jsonl'),'pair_seer')
    import z3
    solver=z3.Solver();solver.add(z3.BoolVal(True));assert solver.check()==z3.sat
    solver.add(z3.BoolVal(False));assert solver.check()==z3.unsat
    collector=Collector(batch,root,deadline,conn,journal)
    collector.offer({'t':[{'x':1}]})
    time.sleep(60)


def test_real_solver_and_original_query_calls_survive_worker_cutoff(tmp_path):
    schema=from_ddl('entry-metrics','CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);',dialect='sqlite')
    (tmp_path/'schema.json').write_text(json.dumps(schema.to_json()))
    batch={'batch_id':'entry-spec','dataset':'spec','dialect':'sqlite','schema_path':'schema.json','queries':[{'qid':'all','sql':'SELECT x FROM t'},{'qid':'none','sql':'SELECT x FROM t WHERE 0'}]}
    row=supervise(batch,str(tmp_path),'pair_seer',3,0,tmp_path/'journal',worker=measured_worker)
    assert row['source_status']=='deadline'
    assert row['call_metrics']['counts']['python_solver_check_attempt']==2
    assert row['call_metrics']['counts']['common_query_execute_attempt']==6
    assert row['call_metrics']['counts']['common_database_load_attempt']==3
    assert not row['call_metrics']['partial_line']
    batch['_schema']=schema
    assert score_record(batch,row)[0]=={('all','none'):'split'}


def test_partial_last_line_is_retained_as_incomplete_not_filled(tmp_path):
    path=tmp_path/'entries.jsonl'
    entry={'kind':'generation_api_start','pid':os.getpid(),'t':2.0,'role':'test','call_id':None}
    path.write_bytes((json.dumps(entry)+'\n{"kind":"generation_api_st').encode())
    result=read_entries(path,1,3)
    assert result['partial_line'] and result['counts']=={'generation_api_start':1}
    assert len(result['entries'])==1
    assert path.read_bytes().endswith(b'api_st')


def test_after_cutoff_entry_is_visible_but_not_counted_in_window(tmp_path):
    path=tmp_path/'entries.jsonl'
    path.write_text(''.join(json.dumps({'kind':'python_solver_check_attempt','pid':1,'t':t,'role':'test','call_id':None})+'\n' for t in (1.5,3.1)))
    result=read_entries(path,1,3)
    assert result['counts']=={'python_solver_check_attempt':1}
    assert [r['within_acceptance_window'] for r in result['entries']]==[True,False]
