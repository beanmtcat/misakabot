#!/usr/bin/env bash
# Migrate an already-upgraded MoviePilot V3 SQLite database to PostgreSQL.
#
# The script deliberately defaults to a non-mutating preflight.  Use --apply
# only after reviewing the paths it prints.  It never deletes user.db.
set -Eeuo pipefail

CONFIG_DIR="/data/moviepilotv2/moviepilot"
COMPOSE_FILE="${HOME}/docker/docker-compose.yml"
MOVIEPILOT_SERVICE="moviepilotv3"
POSTGRES_CONTAINER="postgres"
DATABASE="moviepilot"
DATABASE_USER="moviepilot"
PGLOADER_JAR_URL="https://github.com/dimitri/pgloader/releases/download/v4-dev/pgloader.jar"
PGLOADER_JAVA_IMAGE="eclipse-temurin:21-jre"
PGLOADER_CURL_IMAGE="curlimages/curl:8.16.0"
APPLY=0
WORK_DIR=""
CONFIG_BACKUP=""
SERVICE_WAS_RUNNING=0
CONFIG_CHANGED=0
COMPLETED=0

usage() {
  cat <<'EOF'
Usage:
  migrate_v3_sqlite_to_postgres.sh [options]

Options:
  --apply                       Execute the migration. Without this, only preflight runs.
  --config-dir PATH             MoviePilot /config directory.
  --compose-file PATH           Docker Compose file containing MoviePilot V3.
  --moviepilot-service NAME     Compose service name (default: moviepilotv3).
  --postgres-container NAME     PostgreSQL container name (default: postgres).
  --database NAME               New PostgreSQL database name (default: moviepilot).
  --database-user NAME          New PostgreSQL login role (default: moviepilot).
  -h, --help                    Show this help.

The target database must not already exist. The script creates a timestamped
working directory below the config directory containing an SQLite snapshot,
the original app.env and migration logs. Do not delete it until V3 has been
running normally for several days.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '==> %s\n' "$*"; }

validate_identifier() {
  [[ "$1" =~ ^[a-z][a-z0-9_]{0,62}$ ]] || die "Invalid PostgreSQL identifier: $1"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --config-dir) CONFIG_DIR=${2:?missing value}; shift ;;
    --compose-file) COMPOSE_FILE=${2:?missing value}; shift ;;
    --moviepilot-service) MOVIEPILOT_SERVICE=${2:?missing value}; shift ;;
    --postgres-container) POSTGRES_CONTAINER=${2:?missing value}; shift ;;
    --database) DATABASE=${2:?missing value}; shift ;;
    --database-user) DATABASE_USER=${2:?missing value}; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
  shift
done

validate_identifier "$DATABASE"
validate_identifier "$DATABASE_USER"

SOURCE_DB="${CONFIG_DIR}/user.db"
APP_ENV="${CONFIG_DIR}/app.env"
COMPOSE=(docker compose -f "$COMPOSE_FILE")

require_preflight() {
  command -v docker >/dev/null || die "docker is required"
  command -v python3 >/dev/null || die "python3 is required for a consistent SQLite snapshot"
  command -v openssl >/dev/null || die "openssl is required to generate the PostgreSQL password"
  [[ -f "$COMPOSE_FILE" ]] || die "Compose file not found: $COMPOSE_FILE"
  [[ -f "$SOURCE_DB" ]] || die "SQLite database not found: $SOURCE_DB"
  [[ -f "$APP_ENV" ]] || die "MoviePilot config not found: $APP_ENV"
  docker inspect "$POSTGRES_CONTAINER" >/dev/null 2>&1 || die "PostgreSQL container not found: $POSTGRES_CONTAINER"
  "${COMPOSE[@]}" config --services | grep -Fxq "$MOVIEPILOT_SERVICE" \
    || die "MoviePilot service not found in Compose: $MOVIEPILOT_SERVICE"
  local database_type
  database_type=$(sed -nE 's/^DB_TYPE=([^[:space:]#]+).*/\1/p' "$APP_ENV" | tail -n 1 | tr '[:upper:]' '[:lower:]')
  [[ -z "$database_type" || "$database_type" == "sqlite" ]] \
    || die "app.env is configured for DB_TYPE=${database_type}; refusing to migrate an unknown source"
}

pg_admin() {
  docker exec -i "$POSTGRES_CONTAINER" sh -ceu \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-$POSTGRES_USER}"'
}

pg_target_admin() {
  docker exec -i "$POSTGRES_CONTAINER" sh -ceu \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"' sh "$DATABASE"
}

database_exists() {
  printf "SELECT 1 FROM pg_database WHERE datname = '%s';\n" "$DATABASE" \
    | pg_admin | grep -qx 1
}

set_env() {
  local key=$1 value=$2 temporary
  temporary=$(mktemp "${APP_ENV}.tmp.XXXXXX")
  awk -v key="$key" -v value="$value" '
    index($0, key "=") == 1 { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$APP_ENV" > "$temporary"
  chmod --reference="$APP_ENV" "$temporary"
  mv "$temporary" "$APP_ENV"
}

wait_for_health() {
  local container status attempt
  for attempt in $(seq 1 90); do
    container=$("${COMPOSE[@]}" ps -q "$MOVIEPILOT_SERVICE")
    if [[ -n "$container" ]]; then
      status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")
      [[ "$status" == "healthy" || "$status" == "running" ]] && return 0
      [[ "$status" == "exited" || "$status" == "dead" ]] && break
    fi
    sleep 2
  done
  die "MoviePilot did not become healthy after database initialization"
}

make_sqlite_snapshot() {
  local destination=$1
  python3 - "$SOURCE_DB" "$destination" <<'PY'
from pathlib import Path
import sqlite3
import sys

source_path = Path(sys.argv[1]).resolve()
destination_path = Path(sys.argv[2])
source = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
destination = sqlite3.connect(destination_path)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
PY
}

write_loader_file() {
  local password=$1
  cat > "${WORK_DIR}/migrate.load" <<EOF
LOAD DATABASE
     FROM sqlite:////work/user.db
     INTO postgresql://${DATABASE_USER}:${password}@127.0.0.1:5432/${DATABASE}

WITH
     data only,
     reset sequences

EXCLUDING TABLE NAMES MATCHING ~/^(userrequest|alembic_version)$/

SET work_mem TO '16MB',
    maintenance_work_mem TO '512MB'

CAST
     type integer to boolean when (= precision 1)
;
EOF
  chmod 600 "${WORK_DIR}/migrate.load"
}

install_pgloader_v4() {
  note "Downloading the official pgloader V4 JAR"
  docker run --rm --user 0:0 --network host -v "${WORK_DIR}:/work" "$PGLOADER_CURL_IMAGE" \
    --fail --location --retry 3 --retry-delay 2 "$PGLOADER_JAR_URL" -o /work/pgloader.jar
  docker run --rm --user 0:0 -v "${WORK_DIR}:/work:ro" "$PGLOADER_JAVA_IMAGE" \
    java -jar /work/pgloader.jar --version | tee "${WORK_DIR}/pgloader-version.log"
  sha256sum "${WORK_DIR}/pgloader.jar" > "${WORK_DIR}/pgloader.jar.sha256"
}

run_pgloader_v4() {
  local loader_status
  set +e
  docker run --rm --user 0:0 --network "container:${POSTGRES_CONTAINER}" -v "${WORK_DIR}:/work:ro" \
    "$PGLOADER_JAVA_IMAGE" sh -ceu '
      cp /work/user.db /tmp/user.db
      sed "s#sqlite:////work/user.db#sqlite:////tmp/user.db#" /work/migrate.load > /tmp/migrate.load
      java --enable-native-access=ALL-UNNAMED -jar /work/pgloader.jar /tmp/migrate.load
    ' 2>&1 \
    | sed -E 's#postgresql://[^@[:space:]]+@#postgresql://<redacted>@#g' \
    | tee "${WORK_DIR}/pgloader.log"
  loader_status=${PIPESTATUS[0]}
  set -e
  return "$loader_status"
}

truncate_initialized_tables() {
  cat <<'SQL' | pg_target_admin
DO $$
DECLARE
  item record;
BEGIN
  FOR item IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE format('TRUNCATE TABLE public.%I RESTART IDENTITY CASCADE', item.tablename);
  END LOOP;
END $$;
SQL
}

reset_sequences() {
  cat <<'SQL' | docker exec -i -e "PGPASSWORD=${DATABASE_PASSWORD}" "$POSTGRES_CONTAINER" \
    psql -v ON_ERROR_STOP=1 -U "$DATABASE_USER" -d "$DATABASE"
DO $$
DECLARE
  item record;
  sequence_name text;
  maximum_id bigint;
BEGIN
  FOR item IN
    SELECT n.nspname AS schema_name,c.relname AS table_name,a.attname AS column_name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    JOIN pg_attribute a ON a.attrelid=c.oid
    WHERE n.nspname='public' AND c.relkind='r' AND a.attnum>0
      AND NOT a.attisdropped
      AND pg_get_serial_sequence(quote_ident(n.nspname)||'.'||quote_ident(c.relname),a.attname) IS NOT NULL
  LOOP
    EXECUTE format('SELECT COALESCE(MAX(%I),0) FROM %I.%I',item.column_name,item.schema_name,item.table_name)
      INTO maximum_id;
    sequence_name := pg_get_serial_sequence(quote_ident(item.schema_name)||'.'||quote_ident(item.table_name),item.column_name);
    PERFORM setval(sequence_name,maximum_id+1,false);
  END LOOP;
END $$;
SQL
}

normalize_snapshot_json() {
  python3 - "${WORK_DIR}/user.db" <<'PY'
import json
import sqlite3
import sys

database = sqlite3.connect(sys.argv[1])

def reject_nonstandard_constant(value):
    raise ValueError(value)

for rowid, value in database.execute(
    "SELECT rowid, value FROM systemconfig WHERE value IS NOT NULL"
):
    try:
        decoded = json.loads(value, parse_constant=reject_nonstandard_constant)
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = value
    canonical = json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))
    database.execute("UPDATE systemconfig SET value = ? WHERE rowid = ?", (canonical, rowid))

database.commit()
database.close()
PY
}

write_sqlite_counts() {
  python3 - "${WORK_DIR}/user.db" "${WORK_DIR}/sqlite-counts.tsv" <<'PY'
import sqlite3
import sys

database = sqlite3.connect(sys.argv[1])
with open(sys.argv[2], "w", encoding="utf-8") as output:
    for (name,) in database.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT IN ('userrequest', 'alembic_version') ORDER BY name"
    ):
        escaped = name.replace('"', '""')
        count = database.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
        output.write(f"{name}\t{count}\n")
database.close()
PY
}

verify_counts() {
  local table sqlite_count pg_count quoted_table mismatches=0
  while IFS=$'\t' read -r table sqlite_count; do
    quoted_table=${table//\"/\"\"}
    if ! pg_count=$(printf 'SELECT COUNT(*) FROM public."%s";\n' "$quoted_table" \
      | docker exec -i -e "PGPASSWORD=${DATABASE_PASSWORD}" "$POSTGRES_CONTAINER" \
        psql -v ON_ERROR_STOP=1 -At -U "$DATABASE_USER" -d "$DATABASE" 2>/dev/null); then
      printf 'missing target table: %s\n' "$table" >&2
      mismatches=$((mismatches + 1))
      continue
    fi
    if [[ "$sqlite_count" != "$pg_count" ]]; then
      printf 'row-count mismatch: %s SQLite=%s PostgreSQL=%s\n' "$table" "$sqlite_count" "$pg_count" >&2
      mismatches=$((mismatches + 1))
    fi
  done < "${WORK_DIR}/sqlite-counts.tsv"
  [[ "$mismatches" -eq 0 ]] || die "PostgreSQL verification failed (${mismatches} mismatched table(s))"
}

restore_sqlite_on_failure() {
  local status=$?
  if [[ "$status" -ne 0 && "$APPLY" -eq 1 && "$COMPLETED" -eq 0 && -n "$CONFIG_BACKUP" && -f "$CONFIG_BACKUP" ]]; then
    printf 'Migration failed; restoring SQLite configuration and restarting MoviePilot.\n' >&2
    cp -f "$CONFIG_BACKUP" "$APP_ENV"
    if [[ "$SERVICE_WAS_RUNNING" -eq 1 ]]; then
      "${COMPOSE[@]}" up -d "$MOVIEPILOT_SERVICE" >/dev/null 2>&1 || true
    fi
  fi
  trap - EXIT
  exit "$status"
}
trap restore_sqlite_on_failure EXIT

require_preflight
note "Source SQLite: $SOURCE_DB"
note "MoviePilot config: $APP_ENV"
note "PostgreSQL container: $POSTGRES_CONTAINER"
note "Target database: $DATABASE (role: $DATABASE_USER)"

if database_exists; then
  die "Target database already exists: $DATABASE (the script will not overwrite it)"
fi

if [[ "$APPLY" -ne 1 ]]; then
  note "Preflight passed. Run again with --apply to migrate."
  exit 0
fi

if "${COMPOSE[@]}" ps --services --status running | grep -Fxq "$MOVIEPILOT_SERVICE"; then
  SERVICE_WAS_RUNNING=1
fi

WORK_DIR="${CONFIG_DIR}/migration-postgres-$(date +%Y%m%d-%H%M%S)"
umask 077
mkdir -p "$WORK_DIR"
CONFIG_BACKUP="${WORK_DIR}/app.env.sqlite.before"
cp -p "$APP_ENV" "$CONFIG_BACKUP"

note "Stopping MoviePilot and creating a consistent SQLite snapshot"
"${COMPOSE[@]}" stop "$MOVIEPILOT_SERVICE"
make_sqlite_snapshot "${WORK_DIR}/user.db"
normalize_snapshot_json
write_sqlite_counts

DATABASE_PASSWORD=$(openssl rand -hex 32)
note "Creating PostgreSQL role and empty database"
printf "CREATE ROLE %s LOGIN PASSWORD '%s';\nCREATE DATABASE %s OWNER %s ENCODING 'UTF8' TEMPLATE template0;\n" \
  "$DATABASE_USER" "$DATABASE_PASSWORD" "$DATABASE" "$DATABASE_USER" | pg_admin

note "Switching MoviePilot to PostgreSQL once to initialize its V3 schema"
set_env DB_TYPE postgresql
set_env DB_POSTGRESQL_HOST postgres
set_env DB_POSTGRESQL_PORT 5432
set_env DB_POSTGRESQL_DATABASE "$DATABASE"
set_env DB_POSTGRESQL_USERNAME "$DATABASE_USER"
set_env DB_POSTGRESQL_PASSWORD "$DATABASE_PASSWORD"
CONFIG_CHANGED=1
"${COMPOSE[@]}" up -d "$MOVIEPILOT_SERVICE"
wait_for_health
"${COMPOSE[@]}" stop "$MOVIEPILOT_SERVICE"

note "Clearing only V3 initialization rows; table structure remains intact"
truncate_initialized_tables
write_loader_file "$DATABASE_PASSWORD"

install_pgloader_v4
note "Importing SQLite data with pgloader V4"
run_pgloader_v4

note "Repairing PostgreSQL sequences and validating imported row counts"
reset_sequences
verify_counts

note "Starting MoviePilot V3 on PostgreSQL"
"${COMPOSE[@]}" up -d "$MOVIEPILOT_SERVICE"
wait_for_health
COMPLETED=1

note "Migration complete. SQLite was preserved at: $SOURCE_DB"
note "Migration evidence and the original SQLite app.env are in: $WORK_DIR"
