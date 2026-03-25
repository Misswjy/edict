#!/usr/bin/env bash
set -euo pipefail

EXECUTE=0
STAGE="${STAGE:-5}"
BACKUP_DIR="${BACKUP_DIR:-}"
COMPOSE_FILE="${COMPOSE_FILE:-edict/docker-compose.yml}"
FRONTEND_ENV_FILE="${FRONTEND_ENV_FILE:-edict/frontend/.env.production.local}"
V2_API_URL="${V2_API_URL:-http://localhost:8000}"
LEGACY_UPSTREAM_URL="${LEGACY_UPSTREAM_URL:-http://host.docker.internal:7891}"
V2_UPSTREAM_URL="${V2_UPSTREAM_URL:-http://backend:8000}"
ROUTING_CONFIG_FILE="${ROUTING_CONFIG_FILE:-ops/cutover/generated/frontend-default.conf}"
MANIFEST_FILE="${MANIFEST_FILE:-ops/cutover/generated/stage-manifest.json}"
FREEZE_MARKER="${FREEZE_MARKER:-ops/cutover/.legacy_write_frozen}"
LEGACY_LOOP_PATTERN="${LEGACY_LOOP_PATTERN:-scripts/run_loop.sh}"
ROUTING_HELPER="${ROUTING_HELPER:-ops/cutover/render_stage_routing.py}"

usage() {
  cat <<'EOF'
Usage: cutover_to_v2.sh [options]

Options:
  --execute                     Perform real cutover actions (default is dry-run)
  --stage <1-5>                 Cutover stage to apply (default: 5)
  --backup-dir <dir>            Existing backup directory created before cutover
  --compose-file <file>         v2 compose file (default: edict/docker-compose.yml)
  --frontend-env-file <file>    Frontend production env file to pin same-origin /api routing
  --v2-api-url <url>            Externally reachable v2 API URL for smoke checks (default: http://localhost:8000)
  --legacy-upstream-url <url>   Legacy upstream reachable from frontend container
  --v2-upstream-url <url>       v2 upstream reachable from frontend container
  --routing-config-file <file>  Generated frontend nginx config file
  --manifest-file <file>        Generated stage manifest JSON file
  --freeze-marker <file>        Marker file for frozen legacy write traffic
  --legacy-loop-pattern <pat>   Process pattern for stopping legacy loop at Stage 4/5
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
    --stage)
      STAGE="${2:?missing value}"
      shift 2
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
    --freeze-marker)
      FREEZE_MARKER="${2:?missing value}"
      shift 2
      ;;
    --legacy-loop-pattern)
      LEGACY_LOOP_PATTERN="${2:?missing value}"
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

if [[ ! "$STAGE" =~ ^[1-5]$ ]]; then
  log "ERROR: --stage must be between 1 and 5"
  exit 1
fi

if [[ ! -f "$ROUTING_HELPER" ]]; then
  log "ERROR: routing helper not found: $ROUTING_HELPER"
  exit 1
fi

helper_cmd=(
  python3 "$ROUTING_HELPER"
  --stage "$STAGE"
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
log "stage=$STAGE ($STAGE_NAME)"
if [[ -n "$BACKUP_DIR" ]]; then
  if [[ ! -d "$BACKUP_DIR" ]]; then
    log "ERROR: --backup-dir not found: $BACKUP_DIR"
    exit 1
  fi
  log "using backup_dir=$BACKUP_DIR"
else
  log "WARN: --backup-dir not provided. Run backup before real cutover."
fi

log "phase 1/5: render stage routing and frontend same-origin API base"
if [[ "$EXECUTE" -eq 1 ]]; then
  mkdir -p "$(dirname "$ROUTING_CONFIG_FILE")"
  mkdir -p "$(dirname "$MANIFEST_FILE")"
  mkdir -p "$(dirname "$FRONTEND_ENV_FILE")"
  printf 'VITE_API_URL=%s\n' "$FRONTEND_API_BASE" > "$FRONTEND_ENV_FILE"
  log "routing config updated: $ROUTING_CONFIG_FILE"
  log "stage manifest updated: $MANIFEST_FILE"
  log "frontend env updated: $FRONTEND_ENV_FILE -> VITE_API_URL=$FRONTEND_API_BASE"
else
  log "[dry-run] render stage routing to $ROUTING_CONFIG_FILE"
  log "[dry-run] write VITE_API_URL=$FRONTEND_API_BASE to $FRONTEND_ENV_FILE"
fi

log "phase 2/5: converge v2 compose services for stage"
if [[ ! -f "$COMPOSE_FILE" ]]; then
  log "ERROR: compose file not found: $COMPOSE_FILE"
  exit 1
fi
read -r -a compose_up <<< "${COMPOSE_UP_SERVICES:-}"
read -r -a compose_stop <<< "${COMPOSE_STOP_SERVICES:-}"
if [[ "${#compose_up[@]}" -gt 0 ]]; then
  run_cmd docker compose -f "$COMPOSE_FILE" up -d --build "${compose_up[@]}"
fi
if [[ "${#compose_stop[@]}" -gt 0 ]]; then
  run_cmd docker compose -f "$COMPOSE_FILE" stop "${compose_stop[@]}"
fi

log "phase 3/5: apply legacy guard rails"
run_cmd mkdir -p "$(dirname "$FREEZE_MARKER")"
if [[ "$FREEZE_LEGACY_WRITES" -eq 1 ]]; then
  run_cmd touch "$FREEZE_MARKER"
else
  run_cmd rm -f "$FREEZE_MARKER"
fi
if [[ "$STOP_LEGACY_LOOP" -eq 1 ]]; then
  if [[ "$EXECUTE" -eq 1 ]]; then
    log "+ pkill -f '$LEGACY_LOOP_PATTERN' || true"
    pkill -f "$LEGACY_LOOP_PATTERN" || true
  else
    log "[dry-run] pkill -f '$LEGACY_LOOP_PATTERN' || true"
  fi
else
  log "legacy loop remains active for this stage"
fi

log "phase 4/5: stage summary"
log "compose_up=${COMPOSE_UP_SERVICES:-<none>}"
log "compose_stop=${COMPOSE_STOP_SERVICES:-<none>}"
log "freeze_legacy_writes=$FREEZE_LEGACY_WRITES"
log "stop_legacy_loop=$STOP_LEGACY_LOOP"
log "legacy_observation=$LEGACY_OBSERVATION"

log "phase 5/5: post-cutover smoke checks"
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
