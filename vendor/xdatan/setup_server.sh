#!/bin/bash
# Native (no-docker) install of XDataN (Somwase, Das, Sudarshan, SIGMOD'26) on eval-server.
# Pinned to gitlab.com/xdata/xdatapublic @ a860311 (no LICENSE file in that repo).
# Same artefacts as the authors' Dockerfile: xdata.jar + bundled jars, bundled z3 binary,
# OpenJDK 11, PostgreSQL 16 (Ubuntu 24.10 ships PG16; we use pgserver's PG 16.2), jpype1.
# Differences: no docker (server root fs is 97% full), PG cluster bound to 127.0.0.1:$XD_PORT,
# password auth with a locally generated secret (never committed or printed).
set -euo pipefail
ROOT=${XD_ROOT:-${SEER_ROOT}/xdatan}
XD_PORT=${XD_PORT:-55930}
PGBIN=${SEER_ROOT}/venv-pgsrv/lib/python3.10/site-packages/pgserver/pginstall/bin
COMMIT=a860311
mkdir -p "$ROOT" && cd "$ROOT"
echo "setup start $(date -Is) $(uptime)"

if [ ! -d src/.git ]; then
  git clone -q https://gitlab.com/xdata/xdatapublic.git src
fi
git -C src checkout -q $COMMIT
git -C src rev-parse HEAD > src.commit

if [ ! -x jdk/bin/java ]; then
  curl -sSL -o jdk.tgz https://mirrors.tuna.tsinghua.edu.cn/Adoptium/11/jdk/x64/linux/OpenJDK11U-jdk_x64_linux_hotspot_11.0.32.1_1.tar.gz
  mkdir -p jdk && tar -xzf jdk.tgz -C jdk --strip-components=1 && rm jdk.tgz
fi
jdk/bin/java -version 2>&1 | head -1

if [ ! -x venv/bin/python ]; then
  python3 -m venv venv
  venv/bin/pip install -q -r src/installer/requirements.txt
fi
venv/bin/python -c "import jpype; print('jpype', jpype.__version__)"

chmod +x src/xdata/z3/z3
src/xdata/z3/z3 --version

if [ ! -f secret ]; then
  umask 077; python3 -c "import secrets; print(secrets.token_hex(16))" > secret
fi
if [ ! -d pgdata ]; then
  "$PGBIN/initdb" -D pgdata -U postgres --auth=trust >/dev/null
  cat >> pgdata/postgresql.conf <<EOF
listen_addresses = '127.0.0.1'
port = $XD_PORT
unix_socket_directories = '$ROOT'
max_connections = 400
shared_buffers = 2GB
statement_timeout = '120s'
temp_file_limit = '20GB'
EOF
  # local socket trust (owner only; socket dir is ours); TCP needs password
  cat > pgdata/pg_hba.conf <<EOF
local all postgres trust
host  all all 127.0.0.1/32 scram-sha-256
EOF
fi
"$PGBIN/pg_ctl" -D pgdata status >/dev/null 2>&1 || "$PGBIN/pg_ctl" -D pgdata -l pg.log -w start >/dev/null
PW=$(cat secret)
"$PGBIN/psql" -h "$ROOT" -p $XD_PORT -U postgres -qtA postgres <<EOF
DO \$\$BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='xdatauser') THEN CREATE ROLE xdatauser LOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='testuser') THEN CREATE ROLE testuser LOGIN CREATEDB; END IF;
END\$\$;
ALTER ROLE xdatauser PASSWORD '$PW';
ALTER ROLE testuser PASSWORD '$PW';
EOF
"$PGBIN/psql" -h "$ROOT" -p $XD_PORT -U postgres -qtA postgres -c "select 1 from pg_database where datname='xdatadb'" | grep -q 1 \
  || "$PGBIN/createdb" -h "$ROOT" -p $XD_PORT -U postgres -O xdatauser xdatadb
echo "setup done $(date -Is) $(uptime)"
