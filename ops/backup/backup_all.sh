#!/usr/bin/env bash
set -euo pipefail

EXECUTE=0
OUTPUT_ROOT="${OUTPUT_ROOT:-ops/backups}"
DATA_DIR="${DATA_DIR:-data}"
PG_DSN="${PG_DSN:-postgresql://edict:edict_dev_2024@127.0.0.1:5432/edict}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
REDIS_CONTAINER="${REDIS_CONTAINER:-}"
LEGACY_IMAGE="${LEGACY_IMAGE:-}"
COMPOSE_FILE="${COMPOSE_FILE:-edict/docker-compose.yml}"
INCLUDE_IMAGE_TAR=0

usage() {
  cat <<'EOF'
Usage: backup_all.sh [options]

Options:
  --execute                     Perform real backup actions (default is dry-run)
  --output-root <dir>           Backup root directory (default: ops/backups)
  --data-dir <dir>              Legacy data directory (default: data)
  --pg-dsn <dsn>                Postgres DSN for pg_dump
  --redis-url <url>             Redis URL for redis-cli
  --redis-container <name>      Optional docker container name for redis-cli fallback
  --legacy-image <ref>          Legacy image/tag reference to snapshot
  --include-image-tar           Save docker image tarball when --legacy-image is set
  --compose-file <file>         v2 compose file (default: edict/docker-compose.yml)
  -h, --help                    Show this help

Examples:
  bash ops/backup/backup_all.sh
  bash ops/backup/backup_all.sh --execute --legacy-image cft0808/sansheng-demo:latest
EOF
}

log() {
  printf '[backup] %s\n' "$*"
}

run_cmd() {
  if [[ "$EXECUTE" -eq 1 ]]; then
    log "+ $*"
    "$@"
  else
    log "[dry-run] $*"
  fi
}

resolve_redis_container() {
  if [[ -n "$REDIS_CONTAINER" ]]; then
    printf '%s\n' "$REDIS_CONTAINER"
    return 0
  fi

  if ! command -v docker >/dev/null 2>&1; then
    return 1
  fi

  local redis_port=""
  if [[ "$REDIS_URL" =~ ^redis://[^:/?#]+:([0-9]+) ]]; then
    redis_port="${BASH_REMATCH[1]}"
  fi
  if [[ -z "$redis_port" ]]; then
    return 1
  fi

  local matched_container=""
  local container_name=""
  while IFS= read -r container_name; do
    [[ -n "$container_name" ]] || continue
    local port_mapping=""
    port_mapping="$(docker port "$container_name" 6379/tcp 2>/dev/null || true)"
    if printf '%s\n' "$port_mapping" | grep -Eq "[:.]${redis_port}$"; then
      if [[ -n "$matched_container" ]]; then
        return 1
      fi
      matched_container="$container_name"
    fi
  done < <(docker ps --format '{{.Names}}')

  if [[ -n "$matched_container" ]]; then
    printf '%s\n' "$matched_container"
    return 0
  fi
  return 1
}

backup_redis_with_container() {
  local container_name="$1"
  if [[ "$EXECUTE" -eq 1 ]]; then
    log "+ docker exec $container_name redis-cli INFO persistence > $BACKUP_DIR/redis/persistence_info.txt"
    docker exec "$container_name" redis-cli INFO persistence > "$BACKUP_DIR/redis/persistence_info.txt" || true
    log "+ docker exec $container_name sh -lc 'tmp=/tmp/redis-backup.\$\$.rdb; rm -f \"\$tmp\"; redis-cli --rdb \"\$tmp\" >/dev/null 2>&1 && cat \"\$tmp\" && rm -f \"\$tmp\"' > $BACKUP_DIR/redis/dump.rdb"
    docker exec "$container_name" sh -lc 'tmp=/tmp/redis-backup.$$.rdb; rm -f "$tmp"; redis-cli --rdb "$tmp" >/dev/null 2>&1 && cat "$tmp" && rm -f "$tmp"' > "$BACKUP_DIR/redis/dump.rdb" || true
  else
    log "[dry-run] docker exec $container_name redis-cli INFO persistence > $BACKUP_DIR/redis/persistence_info.txt"
    log "[dry-run] docker exec $container_name sh -lc 'tmp=/tmp/redis-backup.\$\$.rdb; rm -f \"\$tmp\"; redis-cli --rdb \"\$tmp\" >/dev/null 2>&1 && cat \"\$tmp\" && rm -f \"\$tmp\"' > $BACKUP_DIR/redis/dump.rdb"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute)
      EXECUTE=1
      shift
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
    --redis-url)
      REDIS_URL="${2:?missing value}"
      shift 2
      ;;
    --redis-container)
      REDIS_CONTAINER="${2:?missing value}"
      shift 2
      ;;
    --legacy-image)
      LEGACY_IMAGE="${2:?missing value}"
      shift 2
      ;;
    --include-image-tar)
      INCLUDE_IMAGE_TAR=1
      shift
      ;;
    --compose-file)
      COMPOSE_FILE="${2:?missing value}"
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

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${OUTPUT_ROOT%/}/${TIMESTAMP}"

log "mode=$( [[ "$EXECUTE" -eq 1 ]] && echo execute || echo dry-run )"
log "backup_dir=$BACKUP_DIR"

if [[ "$EXECUTE" -eq 1 ]]; then
  mkdir -p "$BACKUP_DIR/data" "$BACKUP_DIR/postgres" "$BACKUP_DIR/redis" "$BACKUP_DIR/metadata"
fi

if [[ -d "$DATA_DIR" ]]; then
  run_cmd tar -czf "$BACKUP_DIR/data/data.tar.gz" -C "$DATA_DIR" .
else
  log "WARN: data directory not found: $DATA_DIR"
fi

if command -v pg_dump >/dev/null 2>&1; then
  run_cmd pg_dump --format=custom --file "$BACKUP_DIR/postgres/edict.dump" "$PG_DSN"
else
  log "WARN: pg_dump not found, skipping Postgres backup"
fi

if command -v redis-cli >/dev/null 2>&1; then
  if [[ "$EXECUTE" -eq 1 ]]; then
    log "+ redis-cli -u $REDIS_URL INFO persistence > $BACKUP_DIR/redis/persistence_info.txt"
    redis-cli -u "$REDIS_URL" INFO persistence > "$BACKUP_DIR/redis/persistence_info.txt" || true
    log "+ redis-cli -u $REDIS_URL --rdb $BACKUP_DIR/redis/dump.rdb"
    redis-cli -u "$REDIS_URL" --rdb "$BACKUP_DIR/redis/dump.rdb" >/dev/null 2>&1 || true
  else
    log "[dry-run] redis-cli -u $REDIS_URL INFO persistence > $BACKUP_DIR/redis/persistence_info.txt"
    log "[dry-run] redis-cli -u $REDIS_URL --rdb $BACKUP_DIR/redis/dump.rdb"
  fi
else
  REDIS_CONTAINER_FALLBACK="$(resolve_redis_container || true)"
  if [[ -n "$REDIS_CONTAINER_FALLBACK" ]]; then
    log "redis-cli not found on host; using docker fallback via container: $REDIS_CONTAINER_FALLBACK"
    backup_redis_with_container "$REDIS_CONTAINER_FALLBACK"
  else
    log "WARN: redis-cli not found, skipping Redis snapshot"
  fi
fi

if [[ -f "$COMPOSE_FILE" ]]; then
  if command -v docker >/dev/null 2>&1; then
    run_cmd docker compose -f "$COMPOSE_FILE" config
    if [[ "$EXECUTE" -eq 1 ]]; then
      docker compose -f "$COMPOSE_FILE" config > "$BACKUP_DIR/metadata/compose.snapshot.yml" || true
    fi
  else
    log "WARN: docker not found, skipping compose snapshot"
  fi
else
  log "WARN: compose file not found: $COMPOSE_FILE"
fi

if [[ "$EXECUTE" -eq 1 ]]; then
  git rev-parse HEAD > "$BACKUP_DIR/metadata/git.commit" 2>/dev/null || true
  git tag --points-at HEAD > "$BACKUP_DIR/metadata/git.tags" 2>/dev/null || true
fi

if [[ -n "$LEGACY_IMAGE" ]]; then
  if command -v docker >/dev/null 2>&1; then
    if [[ "$EXECUTE" -eq 1 ]]; then
      log "+ docker image inspect $LEGACY_IMAGE > $BACKUP_DIR/metadata/legacy.image.inspect.json"
      docker image inspect "$LEGACY_IMAGE" > "$BACKUP_DIR/metadata/legacy.image.inspect.json" || true
      if [[ "$INCLUDE_IMAGE_TAR" -eq 1 ]]; then
        run_cmd docker save -o "$BACKUP_DIR/metadata/legacy.image.tar" "$LEGACY_IMAGE"
      fi
    else
      log "[dry-run] docker image inspect $LEGACY_IMAGE > $BACKUP_DIR/metadata/legacy.image.inspect.json"
      if [[ "$INCLUDE_IMAGE_TAR" -eq 1 ]]; then
        log "[dry-run] docker save -o $BACKUP_DIR/metadata/legacy.image.tar $LEGACY_IMAGE"
      fi
    fi
  else
    log "WARN: docker not found, skipping legacy image snapshot"
  fi
fi

if [[ "$EXECUTE" -eq 1 ]]; then
  cat > "$BACKUP_DIR/metadata/manifest.env" <<EOF
TIMESTAMP=$TIMESTAMP
DATA_ARCHIVE=$BACKUP_DIR/data/data.tar.gz
POSTGRES_DUMP=$BACKUP_DIR/postgres/edict.dump
REDIS_RDB=$BACKUP_DIR/redis/dump.rdb
COMPOSE_SNAPSHOT=$BACKUP_DIR/metadata/compose.snapshot.yml
LEGACY_IMAGE=${LEGACY_IMAGE}
EOF
  log "backup manifest written: $BACKUP_DIR/metadata/manifest.env"
fi

log "done"
