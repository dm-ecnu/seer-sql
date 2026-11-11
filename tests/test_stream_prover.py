from pathlib import Path
import json
import pytest
from seer.prover import SQLSolverRuntime, native_result, prove, sha256

@pytest.mark.parametrize('rc,text,status,verdict', [
 (0,'EQ\n','native_eq','EQ'), (0,'NEQ','not_proved','NEQ'),
 (0,'UNKNOWN','not_proved','UNKNOWN'), (0,'TIMEOUT','timeout','TIMEOUT'),
 (1,'EQ','runtime_error',None), (124,'EQ','timeout',None),
 (137,'EQ','timeout',None), (0,'','protocol_error',None),
 (0,'[EQ]','protocol_error',None), (0,'EQ\nEQ','protocol_error',None)])
def test_result_never_promotes_failed_or_ambiguous_call(rc,text,status,verdict):
 assert native_result(rc,text)=={'status':status,'native_verdict':verdict}

def fake_runtime(tmp_path, body):
 java=tmp_path/'java';java.write_text('#!/usr/bin/python3\n'+body);java.chmod(0o755)
 classes=tmp_path/'classes';classes.mkdir();(classes/'SeerSQLSolver.class').write_bytes(b'test')
 lib=tmp_path/'lib';lib.mkdir()
 for name in ('libz3.so','libz3java.so'):(lib/name).write_bytes(b'test')
 jar=tmp_path/'solver.jar';jar.write_bytes(b'test')
 props=tmp_path/'props';props.write_text('sqlsolver.z3.timeout = 10000\n')
 files=[java,jar,props,classes/'SeerSQLSolver.class',lib/'libz3.so',lib/'libz3java.so',Path('/usr/bin/timeout')]
 return SQLSolverRuntime(str(java),str(jar),str(classes),str(lib),str(props),
                         {str(p.resolve()):sha256(p) for p in files},'fixture')

def test_multiline_inputs_and_receipt_tamper_evidence(tmp_path):
 rt=fake_runtime(tmp_path,"import sys,pathlib\npathlib.Path(sys.argv[-1]).write_text('EQ\\n')\n")
 work=tmp_path/'call';sql='SELECT x\nFROM t -- preserve comment\n'
 r=prove(rt,sql,sql,'CREATE TABLE t(x INT);\n',work,2)
 assert r['status']=='native_eq' and not r['proof_admitted']
 assert (work/'q1.sql').read_bytes()==sql.encode()
 assert r['evidence']['q1.sql']==sha256(work/'q1.sql')
 assert r['request_sha256']==sha256(work/'request.json')
 with pytest.raises(FileExistsError):prove(rt,sql,sql,'',work,2)
 (Path(rt.jar)).write_bytes(b'changed')
 with pytest.raises(ValueError,match='changed'):prove(rt,sql,sql,'',tmp_path/'other',2)
 assert not (tmp_path/'other').exists()

def test_partial_eq_file_after_deadline_not_proof(tmp_path):
 rt=fake_runtime(tmp_path,"import sys,pathlib,time\npathlib.Path(sys.argv[-1]).write_text('EQ\\n')\ntime.sleep(10)\n")
 r=prove(rt,'a','b','ddl',tmp_path/'call',.1)
 assert r['status']=='timeout' and r['native_verdict'] is None

@pytest.mark.parametrize('budget',[0,-1,float('nan'),float('inf')])
def test_bad_budget_never_starts(tmp_path,budget):
 rt=fake_runtime(tmp_path,'')
 with pytest.raises(ValueError):prove(rt,'a','b','ddl',tmp_path/'call',budget)
 assert not (tmp_path/'call').exists()


def test_setup_cost_consumes_remaining_budget(tmp_path,monkeypatch):
 import time
 rt=fake_runtime(tmp_path,"raise RuntimeError('must not start')\n")
 original=SQLSolverRuntime.verify
 def slow_verify(self):
  original(self);time.sleep(.03)
 monkeypatch.setattr(SQLSolverRuntime,'verify',slow_verify)
 r=prove(rt,'a','b','ddl',tmp_path/'call',.01)
 assert r['status']=='timeout' and r['reason']=='budget_exhausted_before_native_launch'
 assert not (tmp_path/'call/stdout.log').exists()

def test_native_ignoring_term_is_killed_and_not_proved(tmp_path):
 rt=fake_runtime(tmp_path,"import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(10)\n")
 r=prove(rt,'a','b','ddl',tmp_path/'call',.2)
 assert r['status']=='timeout' and r['native_verdict'] is None
 assert r['wall_s'] < 2


def test_extra_runtime_jars_are_passed_and_evidence_bound(tmp_path):
 from dataclasses import replace
 rt=fake_runtime(tmp_path,"import sys,pathlib\nassert 'runtime-only.jar' in sys.argv[sys.argv.index('-cp')+1]\npathlib.Path(sys.argv[-1]).write_text('EQ\\n')\n")
 jar=tmp_path/'runtime-only.jar';jar.write_bytes(b'native runtime dependency')
 rt=replace(rt,extra_jars=(str(jar),),artifacts={**rt.artifacts,str(jar):sha256(jar)})
 r=prove(rt,'a','b','ddl',tmp_path/'call',2)
 assert r['status']=='native_eq'
 request=json.loads((tmp_path/'call/request.json').read_text())
 assert request['native_classpath']==[rt.classes,rt.jar,str(jar)]
 assert request['artifacts'][str(jar)]==sha256(jar)
 jar.write_bytes(b'changed runtime dependency')
 with pytest.raises(ValueError,match='changed'):prove(rt,'a','b','ddl',tmp_path/'other',2)
 assert not (tmp_path/'other').exists()


def test_missing_runtime_dependency_fingerprint_is_rejected(tmp_path):
 from dataclasses import replace
 rt=fake_runtime(tmp_path,'')
 jar=tmp_path/'runtime-only.jar';jar.write_bytes(b'native dependency')
 rt=replace(rt,extra_jars=(str(jar),))
 with pytest.raises(ValueError,match='omits'):prove(rt,'a','b','ddl',tmp_path/'call',2)
 assert not (tmp_path/'call').exists()
