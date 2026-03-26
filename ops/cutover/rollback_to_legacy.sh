#!/usr/bin/env bash
set -euo pipefail

EXECUTE=0
SKIP_COMPOSE=0
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
LEGACY_COMPOSE_FILE="${LEGACY_COMPOSE_FILE:-docker-compose.yml}"
LEGACY_SERVICE="${LEGACY_SERVICE:-sansheng-demo}"
LEGACY_START_CMD="${LEGACY_START_CMD:-}"
ROUTING_HELPER="${ROUTING_HELPER:-ops/cutover/render_stage_routing.py}"
FREEZE_V2_CMD="${FREEZE_V2_CMD:-}"
FRONTEND_SWITCH_CMD="${FRONTEND_SWITCH_CMD:-}"

usage() {
  cat <<'EOF'
Usage: rollback_to_legacy.sh [options]

Options:
  --execute                     Perform real rollback actions (default is dry-run)
  --skip-compose                Skip docker compose stop/up steps (useful for local-process rehearsal)
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
  --start-legacy                Start the frozen legacy image/service (execute mode only)
  --legacy-compose-file <file>  Legacy compose file (default: docker-compose.yml)
  --legacy-service <name>       Legacy compose service (default: sansheng-demo)
  --legacy-start-cmd <cmd>      Optional custom legacy startup command (overrides compose start)
  --freeze-v2-cmd <cmd>         Optional custom command used to freeze v2 writes
  --frontend-switch-cmd <cmd>   Optional custom command used to switch frontend to legacy build
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

http_check() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "$url" >/dev/null
    return 0
  fi
  python3 - "$url" <<'PY'
import sys
import urllib.request

url = sys.argv[1]
with urllib.request.urlopen(url, timeout=10) as response:
    if response.status >= 400:
        raise SystemExit(1)
PY
}

run_shell_cmd() {
  local cmd="$1"
  if [[ -z "$cmd" ]]; then
    return 0
  fi
  if [[ "$EXECUTE" -eq 1 ]]; then
    log "+ bash -lc $cmd"
    bash -lc "$cmd"
  else
    log "[dry-run] bash -lc $cmd"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute)
      EXECUTE=1
      shift
      ;;
    --skip-compose)
      SKIP_COMPOSE=1
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
    --legacy-compose-file)
      LEGACY_COMPOSE_FILE="${2:?missing value}"
      shift 2
      ;;
    --legacy-service)
      LEGACY_SERVICE="${2:?missing value}"
      shift 2
      ;;
    --legacy-start-cmd)
      LEGACY_START_CMD="${2:?missing value}"
      shift 2
      ;;
    --freeze-v2-cmd)
      FREEZE_V2_CMD="${2:?missing value}"
      shift 2
      ;;
    --frontend-switch-cmd)
      FRONTEND_SWITCH_CMD="${2:?missing value}"
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
if [[ "$SKIP_COMPOSE" -eq 1 ]]; then
  log "compose stop skipped by --skip-compose"
elif [[ -f "$COMPOSE_FILE" ]]; then
  run_cmd docker compose -f "$COMPOSE_FILE" stop frontend scheduler dispatcher orchestrator backend
else
  log "WARN: compose file not found: $COMPOSE_FILE"
fi
run_shell_cmd "$FREEZE_V2_CMD"

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
reinject_cmd=(bash ops/cutover/reinject_tasks_placeholder.sh --backup-dir "$BACKUP_DIR")
if [[ "$EXECUTE" -eq 1 ]]; then
  reinject_cmd+=(--execute)
fi
run_cmd "${reinject_cmd[@]}"

log "phase 6/7: optional legacy runtime restart"
if [[ "$START_LEGACY" -eq 1 ]]; then
  if [[ -n "$LEGACY_START_CMD" ]]; then
    run_shell_cmd "$LEGACY_START_CMD"
  elif [[ "$SKIP_COMPOSE" -eq 1 ]]; then
    log "legacy compose start skipped by --skip-compose; pass --legacy-start-cmd for local rehearsals."
  elif [[ -f "$LEGACY_COMPOSE_FILE" ]]; then
    run_cmd docker compose -f "$LEGACY_COMPOSE_FILE" up -d "$LEGACY_SERVICE"
  else
    log "WARN: legacy compose file not found: $LEGACY_COMPOSE_FILE"
  fi
else
  log "legacy service is not auto-started. use --start-legacy when needed."
fi

log "phase 7/7: restart frontend in legacy proxy mode"
if [[ "$SKIP_COMPOSE" -eq 1 ]]; then
  log "compose frontend restart skipped by --skip-compose"
elif [[ -f "$COMPOSE_FILE" ]]; then
  run_cmd docker compose -f "$COMPOSE_FILE" up -d --build frontend
else
  log "WARN: compose file not found: $COMPOSE_FILE"
fi
run_shell_cmd "$FRONTEND_SWITCH_CMD"

if [[ "$EXECUTE" -eq 1 ]]; then
  if http_check "$LEGACY_API_URL/healthz" >/dev/null 2>&1; then
    log "legacy health check passed: $LEGACY_API_URL/healthz"
  else
    log "WARN: legacy health check failed: $LEGACY_API_URL/healthz"
  fi
else
  log "[dry-run] curl -fsS $LEGACY_API_URL/healthz"
fi

log "done"
