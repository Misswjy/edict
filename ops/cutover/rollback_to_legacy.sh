#!/usr/bin/env bash
set -euo pipefail

EXECUTE=0
BACKUP_DIR="${BACKUP_DIR:-}"
COMPOSE_FILE="${COMPOSE_FILE:-edict/docker-compose.yml}"
FRONTEND_ENV_FILE="${FRONTEND_ENV_FILE:-edict/frontend/.env.v2-cutover}"
LEGACY_API_URL="${LEGACY_API_URL:-http://127.0.0.1:7891}"
DATA_DIR="${DATA_DIR:-data}"
FREEZE_MARKER="${FREEZE_MARKER:-ops/cutover/.legacy_write_frozen}"
START_LEGACY=0
LEGACY_LOOP_CMD="${LEGACY_LOOP_CMD:-bash scripts/run_loop.sh}"
LEGACY_SERVER_CMD="${LEGACY_SERVER_CMD:-python3 dashboard/server.py}"

usage() {
  cat <<'EOF'
Usage: rollback_to_legacy.sh [options]

Options:
  --execute                     Perform real rollback actions (default is dry-run)
  --backup-dir <dir>            Backup directory to restore
  --compose-file <file>         v2 compose file (default: edict/docker-compose.yml)
  --frontend-env-file <file>    Frontend env file to rewrite VITE_API_URL
  --legacy-api-url <url>        Legacy API URL for frontend
  --data-dir <dir>              Legacy data restore target (default: data)
  --freeze-marker <file>        Marker file created during cutover
  --start-legacy                Start legacy loop/server in background (execute mode only)
  --legacy-loop-cmd <cmd>       Legacy loop start command
  --legacy-server-cmd <cmd>     Legacy server start command
  -h, --help                    Show this help
EOF
}

log() {
  printf '[rollback] %s\n' "$*"
}

run_cmd() {
  if [[ "$EXECUTE" -eq 1 ]]; then
    log "+ $*"
    "$@"
  else
    log "[dry-run] $*"
  fi
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
    --compose-file)
      COMPOSE_FILE="${2:?missing value}"
      shift 2
      ;;
    --frontend-env-file)
      FRONTEND_ENV_FILE="${2:?missing value}"
      shift 2
      ;;
    --legacy-api-url)
      LEGACY_API_URL="${2:?missing value}"
      shift 2
      ;;
    --data-dir)
      DATA_DIR="${2:?missing value}"
      shift 2
      ;;
    --freeze-marker)
      FREEZE_MARKER="${2:?missing value}"
      shift 2
      ;;
    --start-legacy)
      START_LEGACY=1
      shift
      ;;
    --legacy-loop-cmd)
      LEGACY_LOOP_CMD="${2:?missing value}"
      shift 2
      ;;
    --legacy-server-cmd)
      LEGACY_SERVER_CMD="${2:?missing value}"
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

if [[ -z "$BACKUP_DIR" || ! -d "$BACKUP_DIR" ]]; then
  log "ERROR: --backup-dir is required and must exist"
  exit 1
fi

log "mode=$( [[ "$EXECUTE" -eq 1 ]] && echo execute || echo dry-run )"
log "backup_dir=$BACKUP_DIR"

log "phase 1/5: freeze v2 write traffic"
if [[ -f "$COMPOSE_FILE" ]]; then
  run_cmd docker compose -f "$COMPOSE_FILE" stop frontend scheduler dispatcher orchestrator backend
else
  log "WARN: compose file not found: $COMPOSE_FILE"
fi

log "phase 2/5: restore legacy data snapshot"
DATA_ARCHIVE="$BACKUP_DIR/data/data.tar.gz"
if [[ -f "$DATA_ARCHIVE" ]]; then
  run_cmd mkdir -p "$DATA_DIR"
  run_cmd tar -xzf "$DATA_ARCHIVE" -C "$DATA_DIR"
else
  log "WARN: missing data archive: $DATA_ARCHIVE"
fi

log "phase 3/5: switch frontend API target to legacy"
if [[ "$EXECUTE" -eq 1 ]]; then
  mkdir -p "$(dirname "$FRONTEND_ENV_FILE")"
  printf 'VITE_API_URL=%s\n' "$LEGACY_API_URL" > "$FRONTEND_ENV_FILE"
  log "frontend env updated: $FRONTEND_ENV_FILE"
else
  log "[dry-run] write VITE_API_URL=$LEGACY_API_URL to $FRONTEND_ENV_FILE"
fi

log "phase 4/5: unfreeze legacy write marker"
if [[ "$EXECUTE" -eq 1 ]]; then
  rm -f "$FREEZE_MARKER"
else
  log "[dry-run] rm -f $FREEZE_MARKER"
fi

log "phase 5/5: rollback-window data reinjection placeholder"
run_cmd bash ops/cutover/reinject_tasks_placeholder.sh --backup-dir "$BACKUP_DIR"

if [[ "$START_LEGACY" -eq 1 ]]; then
  if [[ "$EXECUTE" -eq 1 ]]; then
    log "+ nohup $LEGACY_LOOP_CMD >/tmp/edict-legacy-loop.log 2>&1 &"
    nohup bash -lc "$LEGACY_LOOP_CMD" >/tmp/edict-legacy-loop.log 2>&1 &
    log "+ nohup $LEGACY_SERVER_CMD >/tmp/edict-legacy-server.log 2>&1 &"
    nohup bash -lc "$LEGACY_SERVER_CMD" >/tmp/edict-legacy-server.log 2>&1 &
  else
    log "[dry-run] nohup $LEGACY_LOOP_CMD >/tmp/edict-legacy-loop.log 2>&1 &"
    log "[dry-run] nohup $LEGACY_SERVER_CMD >/tmp/edict-legacy-server.log 2>&1 &"
  fi
else
  log "legacy processes are not auto-started. use --start-legacy when needed."
fi

log "done"

