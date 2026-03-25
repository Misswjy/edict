#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_DIR="$(mktemp -d)"
DB_NAME="edict_migration_$(date +%s)_$RANDOM"
DB_HOST="${EDICT_MIGRATION_DB_HOST:-127.0.0.1}"
DB_PORT="${EDICT_MIGRATION_DB_PORT:-5432}"
DB_OWNER="${EDICT_MIGRATION_DB_OWNER:-$USER}"
BACKEND_PYTHON="${EDICT_BACKEND_PYTHON:-/tmp/edict-backend-venv/bin/python}"
DATA_FILE="${EDICT_MIGRATION_DATA_FILE:-$ROOT_DIR/data/tasks_source.json}"
REMOTE_ROOT="${EDICT_MIGRATION_REMOTE_ROOT:-$HOME/.openclaw}"

cleanup() {
  dropdb -h "$DB_HOST" -p "$DB_PORT" --if-exists "$DB_NAME" >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

if [[ ! -f "$DATA_FILE" ]]; then
  echo "[error] data file not found: $DATA_FILE"
  exit 1
fi

if ! command -v createdb >/dev/null 2>&1 || ! command -v dropdb >/dev/null 2>&1; then
  echo "[error] createdb/dropdb is required"
  exit 1
fi

if [[ ! -x "$BACKEND_PYTHON" ]]; then
  echo "[error] backend python not found: $BACKEND_PYTHON"
  exit 1
fi

if ! "$BACKEND_PYTHON" -c "import sqlalchemy, asyncpg, alembic" >/dev/null 2>&1; then
  echo "[error] backend python is missing sqlalchemy/asyncpg/alembic: $BACKEND_PYTHON"
  exit 1
fi

ASYNC_URL="postgresql+asyncpg://${DB_OWNER}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
ALEMBIC_INI="$TMP_DIR/alembic.ini"
DRY_REPORT="$TMP_DIR/migration_dry_run.json"
IMPORT_REPORT="$TMP_DIR/migration_import.json"

createdb -h "$DB_HOST" -p "$DB_PORT" "$DB_NAME"
python3 - <<'PY' "$ROOT_DIR/edict/alembic.ini" "$ALEMBIC_INI" "$ASYNC_URL"
from pathlib import Path
import sys

src = Path(sys.argv[1]).read_text(encoding="utf-8")
dst = Path(sys.argv[2])
url = sys.argv[3]
dst.write_text(src.replace(
    "sqlalchemy.url = postgresql+asyncpg://edict:edict_dev_2024@localhost:5432/edict",
    f"sqlalchemy.url = {url}",
), encoding="utf-8")
PY

(
  cd "$ROOT_DIR/edict"
  DATABASE_URL_OVERRIDE="$ASYNC_URL" "$BACKEND_PYTHON" -m alembic -c "$ALEMBIC_INI" upgrade head
)
echo "[ok] alembic upgraded temp db: $DB_NAME"

REMOTE_ARGS=()
if [[ -d "$REMOTE_ROOT" ]]; then
  REMOTE_ARGS+=(--remote-root "$REMOTE_ROOT")
fi

DATABASE_URL_OVERRIDE="$ASYNC_URL" \
  "$BACKEND_PYTHON" "$ROOT_DIR/edict/migration/migrate_json_to_pg.py" \
  --file "$DATA_FILE" \
  --dry-run \
  --report-file "$DRY_REPORT" \
  "${REMOTE_ARGS[@]}"
echo "[ok] migration dry-run report: $DRY_REPORT"

DATABASE_URL_OVERRIDE="$ASYNC_URL" \
  "$BACKEND_PYTHON" "$ROOT_DIR/edict/migration/migrate_json_to_pg.py" \
  --file "$DATA_FILE" \
  --report-file "$IMPORT_REPORT" \
  "${REMOTE_ARGS[@]}"
echo "[ok] migration import report: $IMPORT_REPORT"

python3 - <<'PY' "$DRY_REPORT" "$IMPORT_REPORT"
from pathlib import Path
import json
import sys

dry = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
imp = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

assert dry["reconciliation"]["dryRun"] is True
assert imp["reconciliation"]["dryRun"] is False
assert imp["reconciliation"]["ready"] is True
assert imp["reconciliation"]["tasks"]["sourceMatchesProcessed"] is True

source_total = int(imp["reconciliation"]["tasks"]["sourceTotal"] or 0)
processed_total = int(imp["reconciliation"]["tasks"]["processedTotal"] or 0)
assert source_total == processed_total

print("[ok] migration reconciliation verified")
print(
    json.dumps(
        {
            "sourceTotal": source_total,
            "migrated": imp["stats"]["migrated"],
            "skipped": imp["stats"]["skipped"],
            "errors": imp["stats"]["errors"],
            "sidecars": imp["stats"]["audits"],
        },
        ensure_ascii=False,
    )
)
PY

echo "[ok] validate_migration_flow complete"
