#!/usr/bin/env bash
set -euo pipefail

EXECUTE=0
BACKUP_DIR="${BACKUP_DIR:-}"
COMPOSE_FILE="${COMPOSE_FILE:-edict/docker-compose.yml}"
FRONTEND_ENV_FILE="${FRONTEND_ENV_FILE:-edict/frontend/.env.v2-cutover}"
V2_API_URL="${V2_API_URL:-http://localhost:8000}"
FREEZE_MARKER="${FREEZE_MARKER:-ops/cutover/.legacy_write_frozen}"
LEGACY_LOOP_PATTERN="${LEGACY_LOOP_PATTERN:-scripts/run_loop.sh}"
LEGACY_SERVER_PATTERN="${LEGACY_SERVER_PATTERN:-dashboard/server.py}"

usage() {
  cat <<'EOF'
Usage: cutover_to_v2.sh [options]

Options:
  --execute                     Perform real cutover actions (default is dry-run)
  --backup-dir <dir>            Existing backup directory created before cutover
  --compose-file <file>         v2 compose file (default: edict/docker-compose.yml)
  --frontend-env-file <file>    Frontend env file to write VITE_API_URL
  --v2-api-url <url>            V2 API URL for frontend (default: http://localhost:8000)
  --freeze-marker <file>        Marker file for frozen legacy write traffic
  -h, --help                    Show this help
EOF
}

log() {
  printf '[cutover-v2] %s\n' "$*"
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
    --v2-api-url)
      V2_API_URL="${2:?missing value}"
      shift 2
      ;;
    --freeze-marker)
      FREEZE_MARKER="${2:?missing value}"
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

log "mode=$( [[ "$EXECUTE" -eq 1 ]] && echo execute || echo dry-run )"
if [[ -n "$BACKUP_DIR" ]]; then
  if [[ ! -d "$BACKUP_DIR" ]]; then
    log "ERROR: --backup-dir not found: $BACKUP_DIR"
    exit 1
  fi
  log "using backup_dir=$BACKUP_DIR"
else
  log "WARN: --backup-dir not provided. Run backup before real cutover."
fi

log "phase 1/4: freeze legacy write traffic"
run_cmd mkdir -p "$(dirname "$FREEZE_MARKER")"
run_cmd touch "$FREEZE_MARKER"
if [[ "$EXECUTE" -eq 1 ]]; then
  log "+ pkill -f '$LEGACY_LOOP_PATTERN' || true"
  pkill -f "$LEGACY_LOOP_PATTERN" || true
  log "+ pkill -f '$LEGACY_SERVER_PATTERN' || true"
  pkill -f "$LEGACY_SERVER_PATTERN" || true
else
  log "[dry-run] pkill -f '$LEGACY_LOOP_PATTERN' || true"
  log "[dry-run] pkill -f '$LEGACY_SERVER_PATTERN' || true"
fi

log "phase 2/4: switch frontend API target to v2"
if [[ "$EXECUTE" -eq 1 ]]; then
  mkdir -p "$(dirname "$FRONTEND_ENV_FILE")"
  printf 'VITE_API_URL=%s\n' "$V2_API_URL" > "$FRONTEND_ENV_FILE"
  log "frontend env updated: $FRONTEND_ENV_FILE"
else
  log "[dry-run] write VITE_API_URL=$V2_API_URL to $FRONTEND_ENV_FILE"
fi

log "phase 3/4: switch scheduler/loop chain to v2 workers"
if [[ ! -f "$COMPOSE_FILE" ]]; then
  log "ERROR: compose file not found: $COMPOSE_FILE"
  exit 1
fi
run_cmd docker compose -f "$COMPOSE_FILE" up -d postgres redis backend orchestrator dispatcher scheduler frontend

log "phase 4/4: post-cutover smoke checks"
if [[ "$EXECUTE" -eq 1 ]]; then
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "$V2_API_URL/health" >/dev/null
    log "health check passed: $V2_API_URL/health"
    if curl -fsS "$V2_API_URL/api/admin/health/deep" >/dev/null 2>&1; then
      log "deep health check passed: $V2_API_URL/api/admin/health/deep"
    else
      log "WARN: deep health check failed (auth policy may block this endpoint)"
    fi
  else
    log "WARN: curl not found, skip HTTP checks"
  fi
else
  log "[dry-run] curl -fsS $V2_API_URL/health"
  log "[dry-run] curl -fsS $V2_API_URL/api/admin/health/deep"
fi

log "done"
