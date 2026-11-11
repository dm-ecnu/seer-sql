"""Compile the external observer against an already built native SQLancer jar.

Usage: python build.py --sqlancer-jar /absolute/native/target/sqlancer-2.0.0.jar
  --native-build-result /absolute/native/result.json
  --jdk /absolute/jdk17 --output /absolute/fresh/output
No dependency downloads or native source edits are performed.
"""
from pathlib import Path
import argparse, hashlib, json, os, subprocess, time
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--sqlancer-jar',required=True,type=Path)
p.add_argument('--native-build-result',required=True,type=Path)
p.add_argument('--jdk',required=True,type=Path)
p.add_argument('--output',required=True,type=Path)
a=p.parse_args()
assert all(x.is_absolute() for x in (a.sqlancer_jar,a.native_build_result,a.jdk,a.output))
assert len(os.sched_getaffinity(0))==1, 'run builds on an assigned single server CPU'
root=Path(__file__).resolve().parent
jars=[a.sqlancer_jar,*sorted((a.sqlancer_jar.parent/'lib').glob('*.jar'))]
assert a.sqlancer_jar.is_file() and len(jars)>1,'native package including runtime lib required'
def sha(x):return hashlib.sha256(x.read_bytes()).hexdigest()
native=json.loads(a.native_build_result.read_text())
assert native['source_commit']=='1d797ee53631df2fe574468e2be0425985f39867'
assert native['exit_code']==0 and native['source_unchanged']
assert sha(a.sqlancer_jar)==native['jars'][str(a.sqlancer_jar.relative_to(a.sqlancer_jar.parent.parent))]
a.output.mkdir(parents=True,exist_ok=False);classes=a.output/'classes';classes.mkdir()
inputs=[*sorted((root/'src').rglob('*.java')),root/'resources/META-INF/services/sqlancer.DatabaseProvider',Path(__file__).resolve()]
request={'native_build_result_sha256':sha(a.native_build_result),'sqlancer_commit':native['source_commit'],'native_source_manifest_sha256':native['source_sha256'],'started':time.time(),'affinity':sorted(os.sched_getaffinity(0)), 'sources':{str(x):sha(x) for x in inputs},'native_runtime':{str(x):sha(x) for x in jars}, 'java':{str(a.jdk/'bin'/x):sha(a.jdk/'bin'/x) for x in ('java','javac','jar')}}
(a.output/'request.json').write_text(json.dumps(request,indent=2)+'\n')
# The native shaded jar and JDBC jars retain optional Class-Path entries.
# Check all source warnings strictly; actual runtime loading is tested separately.
with (a.output/'build.log').open('wb') as log:
 subprocess.run([str(a.jdk/'bin/javac'),'-J-XX:ActiveProcessorCount=1','--release','11','-Xlint:all,-path','-Werror','-cp',os.pathsep.join(map(str,jars)),'-d',str(classes),*map(str,sorted((root/'src').rglob('*.java')))],stdout=log,stderr=subprocess.STDOUT,check=True)
 target=classes/'META-INF/services/sqlancer.DatabaseProvider';target.parent.mkdir(parents=True);target.write_bytes((root/'resources/META-INF/services/sqlancer.DatabaseProvider').read_bytes())
 artifact=a.output/'seer-sqlancer-observer.jar'
 subprocess.run([str(a.jdk/'bin/jar'),'-J-XX:ActiveProcessorCount=1','--create','--file',str(artifact),'-C',str(classes),'.'],stdout=log,stderr=subprocess.STDOUT,check=True)
 request.update(finished=time.time(),artifact_sha256=sha(artifact), build_log_sha256=sha(a.output/'build.log'))
(a.output/'result.json').write_text(json.dumps(request,indent=2)+'\n')
print('OBSERVER_COMPILE_SUCCESS '+sha(artifact),flush=True)
