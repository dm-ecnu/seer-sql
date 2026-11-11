"""Bounded development verification, not the paper benchmark reproduction."""
import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
from pathlib import Path
import hashlib,json,os,subprocess,sys
B=Path((SEER_ROOT));W=B/'stream-v2-wt';O=B/'repro/stream-v2-prover-v2'
O.mkdir(exist_ok=False);U=B/'sqldriller/SQLSolver'
java=B/'sqldriller/jdk17/bin/java';javac=java.with_name('javac');jar=U/'build/libs/sqlsolver.jar'
classes=O/'classes';classes.mkdir()
subprocess.run([str(javac),'-cp',str(jar),'-d',str(classes),str(W/'scripts/java/SeerSQLSolver.java')],check=True)
commit=subprocess.check_output(['git','-C',str(U),'rev-parse','HEAD'],text=True).strip()
assert commit=='dcc2a91d8971a4c4d30b055f99d7d8428a1b754b'
assert not subprocess.check_output(['git','-C',str(U),'diff','--name-only','HEAD'],text=True).strip()
sys.path.insert(0,str(W/'src'))
from seer.prover import SQLSolverRuntime,prove,sha256
lib=B/'sqldriller/official_solver/lib';props=U/'sqlsolver.properties'
files=[java.resolve(),jar,props,classes/'SeerSQLSolver.class',lib/'libz3.so',lib/'libz3java.so',Path('/usr/bin/timeout')]
rt=SQLSolverRuntime(str(java),str(jar),str(classes),str(lib),str(props),{str(p):sha256(p) for p in files},commit)
(O/'runtime.json').write_text(json.dumps(rt.__dict__,sort_keys=True,indent=2)+'\n')
# Author README's first example, byte-preserved SQL (multiline in call input).
q1='SELECT i, j\nFROM a'
q2='SELECT T.COL1, T.COL2 FROM (SELECT i AS COL1, j AS COL2 FROM a) AS T'
ddl='CREATE TABLE a ( i INT PRIMARY KEY, j INT, k INT );\nCREATE TABLE b ( x INT PRIMARY KEY, y INT, z INT );'
r=prove(rt,q1,q2,ddl,O/'author-readme',5)
print(json.dumps({'author_readme':r},sort_keys=True),flush=True)
assert r['status']=='native_eq' and r['native_verdict']=='EQ' and not r['proof_admitted'],r
# No native algorithm result is promoted to a final v2 proof before semantic admission.
print('AUTHOR_README_NATIVE_EQ_RECEIPT_PASS',flush=True)
subprocess.run([str(B/'venv-seer/bin/python'),'-m','pytest','-q',str(W/'tests/test_stream_prover.py')],cwd=W,env={**os.environ,'PYTHONPATH':str(W/'src')},check=True)
