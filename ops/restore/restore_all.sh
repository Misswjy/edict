#!/usr/bin/env bash
set -euo pipefail

EXECUTE=0
OUTPUT_ROOT="${OUTPUT_ROOT:-ops/backups}"
BACKUP_DIR="${BACKUP_DIR:-}"
DATA_DIR="${DATA_DIR:-data}"
PG_DSN="${PG_DSN:-postgresql://edict:edict_dev_2024@127.0.0.1:5432/edict}"
REDIS_RDB_TARGET="${REDIS_RDB_TARGET:-}"

usage() {
  cat <<'EOF'
Usage: restore_all.sh [options]

Options:
  --execute                     Perform real restore actions (default is dry-run)
  --backup-dir <dir>            Backup directory to restore (default: latest under ops/backups)
  --output-root <dir>           Backup root directory for latest lookup (default: ops/backups)
  --data-dir <dir>              Target legacy data directory (default: data)
  --pg-dsn <dsn>                Postgres DSN for pg_restore
  --redis-rdb-target <file>     Optional target path for copied Redis RDB snapshot
  -h, --help                    Show this help
EOF
}

log() {
  printf '[restore] %s\n' "$*"
}

run_cmd() {
  if [[ "$EXECUTE" -eq 1 ]]; then
    log "+ $*"
    "$@"
  else
    log "[dry-run] $*"
  fi
}

find_latest_backup() {
  local root="$1"
  find "$root" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | LC_ALL=C sort | tail -n1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute)
      EXECUTE=1
      shift
      ;;
    --backup-dir)
      BACKUP_DIR="${2:?missing value}"
      shift 2
      ;;
    --output-root)
      OUTPUT_ROOT="${2:?missing value}"
      shift 2
      ;;
    --data-dir)
      DATA_DIR="${2:?missing value}"
      shift 2
      ;;
    --pg-dsn)
      PG_DSN="${2:?missing value}"
      shift 2
      ;;
    --redis-rdb-target)
      REDIS_RDB_TARGET="${2:?missing value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      log "Unknown option: $1"
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$BACKUP_DIR" ]]; then
  BACKUP_DIR="$(find_latest_backup "$OUTPUT_ROOT" || true)"
fi

if [[ -z "$BACKUP_DIR" || ! -d "$BACKUP_DIR" ]]; then
  log "ERROR: backup directory not found. use --backup-dir <dir>"
  exit 1
fi

log "mode=$( [[ "$EXECUTE" -eq 1 ]] && echo execute || echo dry-run )"
log "backup_dir=$BACKUP_DIR"

DATA_ARCHIVE="$BACKUP_DIR/data/data.tar.gz"
PG_DUMP="$BACKUP_DIR/postgres/edict.dump"
REDIS_DUMP="$BACKUP_DIR/redis/dump.rdb"

if [[ -f "$DATA_ARCHIVE" ]]; then
  run_cmd mkdir -p "$DATA_DIR"
  run_cmd tar -xzf "$DATA_ARCHIVE" -C "$DATA_DIR"
else
  log "WARN: missing data archive: $DATA_ARCHIVE"
fi

if [[ -f "$PG_DUMP" ]]; then
  if command -v pg_restore >/dev/null 2>&1; then
    run_cmd pg_restore --clean --if-exists --no-owner --no-privileges --dbname "$PG_DSN" "$PG_DUMP"
  else
    log "WARN: pg_restore not found, skipping Postgres restore"
  fi
else
  log "WARN: missing postgres dump: $PG_DUMP"
fi

if [[ -f "$REDIS_DUMP" ]]; then
  if [[ -n "$REDIS_RDB_TARGET" ]]; then
    run_cmd mkdir -p "$(dirname "$REDIS_RDB_TARGET")"
    run_cmd cp "$REDIS_DUMP" "$REDIS_RDB_TARGET"
    log "Redis RDB copied. Restart Redis with this RDB file to apply."
  else
    log "Redis snapshot found: $REDIS_DUMP"
    log "Set --redis-rdb-target <file> to copy snapshot for restore staging."
  fi
else
  log "WARN: missing redis dump: $REDIS_DUMP"
fi

log "done"

