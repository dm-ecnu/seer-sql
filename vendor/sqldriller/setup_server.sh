#!/bin/bash
# Components of the SQLDriller hybrid checker (Yang et al., SIGMOD'25) on eval-server; see
# eval/tools/sqldriller_worker.py for the paper-to-code mapping.
#   SQLSolver   github.com/SJTU-IPADS/SQLSolver @ dcc2a91 (Apache-2.0), fat jar built with JDK 17 + Gradle 7.4
#   VeriEQL     the existing install ${SEER_ROOT}/verieql (+ venv-veq), as for the VeriEQL baseline
#   Test suite  github.com/ruiqi-zhong/TestSuiteEval @ 7bf6378 (fuzz/ + sql_util/, used as released)
#   venv        copy of venv-eval's packages + sqlparse/numpy (TestSuiteEval deps); the worker runs here
set -euo pipefail
ROOT=${SD_ROOT:-${SEER_ROOT}/sqldriller}
SS_COMMIT=dcc2a91d8971a4c4d30b055f99d7d8428a1b754b
TS_COMMIT=7bf637883cf092867d33f58ee6f365f5a05e0ee2
mkdir -p "$ROOT" && cd "$ROOT"
echo "setup start $(date -Is) $(uptime)"
export GRADLE_USER_HOME=$ROOT/gradle-home

[ -d SQLSolver/.git ] || git clone -q https://github.com/SJTU-IPADS/SQLSolver.git SQLSolver
git -C SQLSolver checkout -q $SS_COMMIT
[ -d TestSuiteEval/.git ] || git clone -q https://github.com/ruiqi-zhong/TestSuiteEval.git TestSuiteEval
git -C TestSuiteEval checkout -q $TS_COMMIT

if [ ! -x jdk17/bin/java ]; then
  curl -sSL --retry 8 --retry-all-errors --retry-delay 5 -o jdk17.tgz https://mirrors.tuna.tsinghua.edu.cn/Adoptium/17/jdk/x64/linux/OpenJDK17U-jdk_x64_linux_hotspot_17.0.20.1_1.tar.gz
  mkdir -p jdk17 && tar -xzf jdk17.tgz -C jdk17 --strip-components=1 && rm jdk17.tgz
fi
if [ ! -x gradle-7.4/bin/gradle ]; then
  curl -sSL --retry 8 --retry-all-errors --retry-delay 5 -o gradle.zip https://mirrors.cloud.tencent.com/gradle/gradle-7.4-bin.zip
  python3 -c "import zipfile; zipfile.ZipFile('gradle.zip').extractall('.')" && rm gradle.zip
  chmod +x gradle-7.4/bin/gradle
fi
mkdir -p "$GRADLE_USER_HOME/init.d"
cat > "$GRADLE_USER_HOME/init.d/mirror.gradle" <<'EOF'
settingsEvaluated { s -> s.pluginManagement { repositories {
  maven { url 'https://maven.aliyun.com/repository/gradle-plugin' }
  maven { url 'https://maven.aliyun.com/repository/public' }
  gradlePluginPortal() } } }
allprojects { buildscript { repositories { maven { url 'https://maven.aliyun.com/repository/gradle-plugin' } } }
  repositories { maven { url 'https://maven.aliyun.com/repository/public' } }
  // incremental-compile class analysis writes a btree cache that crawls on server's busy /data disk
  tasks.withType(JavaCompile).configureEach { options.incremental = false } }
EOF
if [ ! -f SQLSolver/build/libs/sqlsolver.jar ]; then
  # server's resolver drops lookups now and then; Gradle keeps what it fetched, so a retry resumes
  for i in 1 2 3 4 5 6 7 8; do
    (cd SQLSolver && JAVA_HOME=$ROOT/jdk17 PATH=$ROOT/jdk17/bin:$PATH taskset -c ${SD_CORES:-0-3} ../gradle-7.4/bin/gradle --no-daemon -q --max-workers=1 -Dorg.gradle.vfs.watch=false \
       -Dorg.gradle.jvmargs=-Xmx3g fatJar > ../gradle.log 2>&1) && break
    echo "gradle attempt $i failed: $(grep -c 'Could not' gradle.log) unresolved"; sleep 10
  done
  ls SQLSolver/build/libs/
  j=$(ls SQLSolver/build/libs/*.jar | head -1); [ "$j" = SQLSolver/build/libs/sqlsolver.jar ] || cp "$j" SQLSolver/build/libs/sqlsolver.jar
fi

if [ ! -x venv/bin/python ]; then
  python3 -m venv venv
  ../venv-eval/bin/pip freeze | grep -v "^-e" > venv-eval.freeze
  venv/bin/pip install -q -r venv-eval.freeze
  venv/bin/pip install -q sqlparse numpy
fi
venv/bin/python -c "import sqlparse, numpy, psycopg2; print('venv ok', sqlparse.__version__)"

# SQLSolver smoke (README example)
mkdir -p smoke && cd smoke
printf 'SELECT i, j FROM a\n' > s1.sql
printf 'SELECT T.COL1, T.COL2 FROM (SELECT i AS COL1, j AS COL2 FROM a) AS T\n' > s2.sql
printf 'CREATE TABLE a ( i INT PRIMARY KEY, j INT, k INT );\n' > sc.sql
LD_LIBRARY_PATH=$ROOT/SQLSolver/lib $ROOT/jdk17/bin/java -Xmx3g -Djava.library.path=$ROOT/SQLSolver/lib \
  -jar $ROOT/SQLSolver/build/libs/sqlsolver.jar -sql1=s1.sql -sql2=s2.sql -schema=sc.sql -print
echo "setup done $(date -Is) $(uptime)"
