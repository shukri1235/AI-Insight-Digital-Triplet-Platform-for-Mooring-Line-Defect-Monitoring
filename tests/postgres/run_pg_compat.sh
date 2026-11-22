#!/usr/bin/env bash
# PG-1: replay the whole test-suite workload on a real PostgreSQL server (Linux; needs the PostgreSQL server
# binaries, e.g. apt install postgresql-16, and psql). No Python PostgreSQL driver is needed: every statement the
# app executes during `pytest tests` is recorded, compiled by SQLAlchemy's PostgreSQL dialect with its values
# (tests/postgres/pg_capture.py), and run through psql against the app schema created for PostgreSQL.
#   tests/postgres/run_pg_compat.sh            -> prints the PostgreSQL errors, if any
# Expected, and ignored: PRAGMA (the WAL test of the SQLite file itself), "already exists" (the suite's own
# create_all is replayed after the schema), and id-based deletes of rows whose SQLite ids differ from PostgreSQL's
# sequence ids (SQLite reuses rowids after deletes; a real PostgreSQL deployment reads its own ids).
set -e
cd "$(dirname "$0")/../.."
PGBIN=${PGBIN:-$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)}
W=${PG_WORK:-/tmp/pgc}; PORT=${PG_PORT:-54329}; mkdir -p "$W/sock"
RUNAS=""; [ "$(id -u)" = "0" ] && { RUNAS="su postgres -s /bin/bash -c"; chown -R postgres "$W"; }
run() { if [ -n "$RUNAS" ]; then $RUNAS "$*"; else bash -c "$*"; fi; }
[ -d "$W/data" ] || run "$PGBIN/initdb -D $W/data -U postgres --auth=trust -E UTF8 >/dev/null"
run "$PGBIN/pg_ctl -D $W/data -o \"-p $PORT -k $W/sock -c listen_addresses=''\" -l $W/pg.log status >/dev/null || $PGBIN/pg_ctl -D $W/data -o \"-p $PORT -k $W/sock -c listen_addresses=''\" -l $W/pg.log -w start"
P="psql -h $W/sock -p $PORT -U postgres -q"
python3 - > "$W/schema.sql" <<'PY'
import os, sys, tempfile
t = tempfile.mkdtemp(); os.environ.update({"FYP_DB_PATH": f"{t}/x.db", "FYP_MEDIA_DIR": f"{t}/m", "FYP_MODELS_DIR": f"{t}/mo",
                                            "FYP_CONFIG": f"{t}/none.toml"})
sys.path.insert(0, "backend")
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy.dialects import postgresql
import database, sensors_db
d = postgresql.dialect()
for meta in (database.Base.metadata, sensors_db.Base.metadata):
    for tb in meta.sorted_tables:
        print(str(CreateTable(tb).compile(dialect=d)).strip() + ";")
        for ix in tb.indexes:
            print(str(CreateIndex(ix).compile(dialect=d)).strip() + ";")
for ddl in database._EXTRA_INDEXES:
    print(ddl + ";")
print("CREATE INDEX IF NOT EXISTS ix_samples_media_t ON sensor_samples (media_id, t);")
PY
PYTHONPATH="${PYTHONPATH}:$PWD/tests/postgres" FYP_CAPTURE_SQL="$W/workload.sql" python3 -m pytest tests -q -p pg_capture | tail -3
$P -c "DROP DATABASE IF EXISTS replay;" -c "CREATE DATABASE replay;"
$P -d replay -v ON_ERROR_STOP=1 -f "$W/schema.sql"
$P -d replay -v ON_ERROR_STOP=0 -o /dev/null -f "$W/workload.sql" 2> "$W/errors.log" || true
echo "statements replayed: $(grep -c ';$' "$W/workload.sql")"
echo "PostgreSQL errors (all):"
grep "ERROR" "$W/errors.log" | sed 's/^psql:[^:]*:[0-9]*: //' | sort | uniq -c | sort -rn
echo "PostgreSQL errors that are NOT the expected replay artefacts:"
grep "ERROR" "$W/errors.log" | grep -v -e 'near "PRAGMA"' -e 'already exists' -e 'defects_video_id_fkey' || echo "  none"
