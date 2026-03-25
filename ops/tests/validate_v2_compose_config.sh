#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "[skip] docker not installed; skip docker compose config validation"
  exit 0
fi

docker compose -f edict/docker-compose.yml config >/dev/null
echo "[ok] docker compose -f edict/docker-compose.yml config"
