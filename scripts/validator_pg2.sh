#!/bin/bash
# Second validator PostgreSQL for wave 2 and for the infrastructure-failure reruns (2026-10-22).
# Same server (pgserver PG 16.2), same locale (en_US.UTF-8, UTF8), same settings as the wave-1 validator
# (${SEER_ROOT}/pg/data: fsync off, trust on 127.0.0.1, lc_* as there); data directory on tmpfs
# because /data on eval-server is a shared spinning disk that other jobs saturate. Catalog autovacuum is made
# more aggressive (validators create and drop a schema per engine, which bloated pg_class/pg_attribute of the
# wave-1 instance to 0.8/3.3 GB). Writes env2.sh = env.sh with the PostgreSQL URLs pointed here.
# /dev/shm does not survive a reboot: rerun this script (idempotent) after one.
set -euo pipefail
SEER=${SEER_ROOT}
PGBIN=$SEER/venv-pgsrv/lib/python3.10/site-packages/pgserver/pginstall/bin
DIR=${PG2_DIR:-/dev/shm/seer-pg2}
PORT=${PG2_PORT:-55932}
mkdir -p "$DIR"; chmod 700 "$DIR"
if [ ! -f "$DIR/data/PG_VERSION" ]; then
  "$PGBIN/initdb" -D "$DIR/data" -U probe --auth=trust --locale=en_US.UTF-8 -E UTF8 \
    --lc-monetary=zh_CN.UTF-8 --lc-numeric=zh_CN.UTF-8 --lc-time=zh_CN.UTF-8 >/dev/null
  cat >> "$DIR/data/postgresql.conf" <<EOF
listen_addresses = '127.0.0.1'
port = $PORT
unix_socket_directories = '$DIR'
max_connections = 400
shared_buffers = 4GB
fsync = off
synchronous_commit = off
full_page_writes = off
timezone = 'Asia/Shanghai'
log_timezone = 'Asia/Shanghai'
datestyle = 'iso, ymd'
autovacuum_naptime = 10s
autovacuum_vacuum_cost_delay = 0
autovacuum_max_workers = 6
EOF
  # same hba as the wave-1 validator: trust on the socket and on loopback only
  printf 'local all all trust\nhost all all 127.0.0.1/32 trust\n' > "$DIR/data/pg_hba.conf"
fi
"$PGBIN/pg_ctl" -D "$DIR/data" status >/dev/null 2>&1 || "$PGBIN/pg_ctl" -D "$DIR/data" -l "$DIR/pg.log" -w start >/dev/null
"$PGBIN/psql" -h "$DIR" -p "$PORT" -U probe -qtA postgres -c "select version()" | cut -c1-40
# env2.sh: env.sh with every PostgreSQL URL/DSN pointed at this instance (MySQL unchanged); never printed
umask 077
sed -E "s#127\.0\.0\.1:55917#127.0.0.1:$PORT#g" "$SEER/env.sh" > "$SEER/env2.sh"
cat >> "$SEER/env2.sh" <<EOF
export PG_URL=postgresql://probe@127.0.0.1:$PORT/postgres
export SEER2_PG_DSN='host=127.0.0.1 port=$PORT dbname=postgres user=probe'
EOF
grep -c "$PORT" "$SEER/env2.sh"
