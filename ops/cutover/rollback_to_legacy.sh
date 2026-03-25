#!/usr/bin/env bash
set -euo pipefail

EXECUTE=0
BACKUP_DIR="${BACKUP_DIR:-}"
COMPOSE_FILE="${COMPOSE_FILE:-edict/docker-compose.yml}"
FRONTEND_ENV_FILE="${FRONTEND_ENV_FILE:-edict/frontend/.env.production.local}"
LEGACY_API_URL="${LEGACY_API_URL:-http://127.0.0.1:7891}"
LEGACY_UPSTREAM_URL="${LEGACY_UPSTREAM_URL:-http://host.docker.internal:7891}"
V2_UPSTREAM_URL="${V2_UPSTREAM_URL:-http://backend:8000}"
ROUTING_CONFIG_FILE="${ROUTING_CONFIG_FILE:-ops/cutover/generated/frontend-default.conf}"
MANIFEST_FILE="${MANIFEST_FILE:-ops/cutover/generated/stage-manifest.json}"
DATA_DIR="${DATA_DIR:-data}"
FREEZE_MARKER="${FREEZE_MARKER:-ops/cutover/.legacy_write_frozen}"
START_LEGACY=0
LEGACY_LOOP_CMD="${LEGACY_LOOP_CMD:-bash scripts/run_loop.sh}"
LEGACY_SERVER_CMD="${LEGACY_SERVER_CMD:-python3 dashboard/server.py}"
ROUTING_HELPER="${ROUTING_HELPER:-ops/cutover/render_stage_routing.py}"

usage() {
  cat <<'EOF'
Usage: rollback_to_legacy.sh [options]

Options:
  --execute                     Perform real rollback actions (default is dry-run)
  --backup-dir <dir>            Backup directory to restore
  --compose-file <file>         v2 compose file (default: edict/docker-compose.yml)
  --frontend-env-file <file>    Frontend production env file to pin same-origin /api routing
  --legacy-api-url <url>        Externally reachable legacy API URL for smoke checks
  --legacy-upstream-url <url>   Legacy upstream reachable from frontend container
  --v2-upstream-url <url>       v2 upstream reachable from frontend container
  --routing-config-file <file>  Generated frontend nginx config file
  --manifest-file <file>        Generated stage manifest JSON file
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
    --legacy-upstream-url)
      LEGACY_UPSTREAM_URL="${2:?missing value}"
      shift 2
      ;;
    --v2-upstream-url)
      V2_UPSTREAM_URL="${2:?missing value}"
      shift 2
      ;;
    --routing-config-file)
      ROUTING_CONFIG_FILE="${2:?missing value}"
      shift 2
      ;;
    --manifest-file)
      MANIFEST_FILE="${2:?missing value}"
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

if [[ ! -f "$ROUTING_HELPER" ]]; then
  log "ERROR: routing helper not found: $ROUTING_HELPER"
  exit 1
fi

helper_cmd=(
  python3 "$ROUTING_HELPER"
  --stage 0
  --legacy-upstream-url "$LEGACY_UPSTREAM_URL"
  --v2-upstream-url "$V2_UPSTREAM_URL"
  --output "$ROUTING_CONFIG_FILE"
  --manifest-file "$MANIFEST_FILE"
  --emit-env
)
if [[ "$EXECUTE" -ne 1 ]]; then
  helper_cmd+=(--dry-run)
fi
eval "$("${helper_cmd[@]}")"

log "mode=$( [[ "$EXECUTE" -eq 1 ]] && echo execute || echo dry-run )"
log "backup_dir=$BACKUP_DIR"

log "phase 1/7: freeze v2 write traffic"
if [[ -f "$COMPOSE_FILE" ]]; then
  run_cmd docker compose -f "$COMPOSE_FILE" stop frontend scheduler dispatcher orchestrator backend
else
  log "WARN: compose file not found: $COMPOSE_FILE"
fi

log "phase 2/7: restore legacy data snapshot"
DATA_ARCHIVE="$BACKUP_DIR/data/data.tar.gz"
if [[ -f "$DATA_ARCHIVE" ]]; then
  run_cmd mkdir -p "$DATA_DIR"
  run_cmd tar -xzf "$DATA_ARCHIVE" -C "$DATA_DIR"
else
  log "WARN: missing data archive: $DATA_ARCHIVE"
fi

log "phase 3/7: render rollback routing and frontend same-origin API base"
if [[ "$EXECUTE" -eq 1 ]]; then
  mkdir -p "$(dirname "$ROUTING_CONFIG_FILE")"
  mkdir -p "$(dirname "$MANIFEST_FILE")"
  mkdir -p "$(dirname "$FRONTEND_ENV_FILE")"
  printf 'VITE_API_URL=%s\n' "$FRONTEND_API_BASE" > "$FRONTEND_ENV_FILE"
  log "routing config updated: $ROUTING_CONFIG_FILE"
  log "stage manifest updated: $MANIFEST_FILE"
  log "frontend env updated: $FRONTEND_ENV_FILE -> VITE_API_URL=$FRONTEND_API_BASE"
else
  log "[dry-run] render legacy routing to $ROUTING_CONFIG_FILE"
  log "[dry-run] write VITE_API_URL=$FRONTEND_API_BASE to $FRONTEND_ENV_FILE"
fi

log "phase 4/7: unfreeze legacy write marker"
run_cmd rm -f "$FREEZE_MARKER"

log "phase 5/7: rollback-window data reinjection placeholder"
run_cmd bash ops/cutover/reinject_tasks_placeholder.sh --backup-dir "$BACKUP_DIR"

log "phase 6/7: optional legacy runtime restart"
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

log "phase 7/7: restart frontend in legacy proxy mode"
if [[ -f "$COMPOSE_FILE" ]]; then
  run_cmd docker compose -f "$COMPOSE_FILE" up -d --build frontend
else
  log "WARN: compose file not found: $COMPOSE_FILE"
fi

if [[ "$EXECUTE" -eq 1 ]]; then
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS "$LEGACY_API_URL/healthz" >/dev/null 2>&1; then
      log "legacy health check passed: $LEGACY_API_URL/healthz"
    else
      log "WARN: legacy health check failed: $LEGACY_API_URL/healthz"
    fi
  else
    log "WARN: curl not found, skip HTTP checks"
  fi
else
  log "[dry-run] curl -fsS $LEGACY_API_URL/healthz"
fi

log "done"
