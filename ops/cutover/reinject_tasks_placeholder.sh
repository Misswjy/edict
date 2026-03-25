#!/usr/bin/env bash
set -euo pipefail

EXECUTE=0
BACKUP_DIR="${BACKUP_DIR:-}"
FROM_TS="${FROM_TS:-}"
TO_TS="${TO_TS:-}"
OUT_PLAN="${OUT_PLAN:-ops/cutover/reinject_plan.md}"

usage() {
  cat <<'EOF'
Usage: reinject_tasks_placeholder.sh [options]

This script does not perform data reinjection yet.
It generates a rollback-window reinjection plan file for manual execution.

Options:
  --execute                     Write plan file (default is dry-run)
  --backup-dir <dir>            Backup directory used for this rollback
  --from <timestamp>            Rollback window start (ISO8601)
  --to <timestamp>              Rollback window end (ISO8601)
  --out-plan <file>             Output markdown plan path
  -h, --help                    Show this help
EOF
}

log() {
  printf '[reinject-placeholder] %s\n' "$*"
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
    --from)
      FROM_TS="${2:?missing value}"
      shift 2
      ;;
    --to)
      TO_TS="${2:?missing value}"
      shift 2
      ;;
    --out-plan)
      OUT_PLAN="${2:?missing value}"
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

if [[ -z "$FROM_TS" ]]; then
  FROM_TS="TODO_SET_ROLLBACK_WINDOW_START"
fi
if [[ -z "$TO_TS" ]]; then
  TO_TS="TODO_SET_ROLLBACK_WINDOW_END"
fi

PLAN_TEMPLATE=$(cat <<'EOF'
# Rollback Window Data Reinjection Plan

- Backup directory: __BACKUP_DIR__
- Window start: __FROM_TS__
- Window end: __TO_TS__

## Goal

Reinject tasks and key metadata that were created or changed in v2 during rollback window, back into legacy data files.

## Suggested Steps (Placeholder)

1. Export v2 changed tasks in window [start, end] from Postgres (tasks.updated_at, task_audits.ts).
2. Build deterministic merge payload keyed by task.id.
3. Validate state/actor/scheduler fields against task_contract.
4. Merge into `data/tasks_source.json` with conflict policy:
   - keep higher `_stateVersion`
   - keep latest `updatedAt`
   - append `flow_log` without duplicates
5. Rebuild derivative files and run parity checks.
6. Preserve audit evidence in an immutable migration log.

## TODO Implementation

- Implement exporter script: ops/cutover/export_v2_delta.py (not created in this scaffold).
- Implement merger script: ops/cutover/merge_delta_into_legacy.py (not created in this scaffold).
- Add parity test: compare legacy live-status and v2 live-status after reinjection.
EOF
)

PLAN_CONTENT="${PLAN_TEMPLATE//__BACKUP_DIR__/${BACKUP_DIR:-TODO_SET_BACKUP_DIR}}"
PLAN_CONTENT="${PLAN_CONTENT//__FROM_TS__/$FROM_TS}"
PLAN_CONTENT="${PLAN_CONTENT//__TO_TS__/$TO_TS}"

if [[ "$EXECUTE" -eq 1 ]]; then
  mkdir -p "$(dirname "$OUT_PLAN")"
  printf '%s\n' "$PLAN_CONTENT" > "$OUT_PLAN"
  log "plan written: $OUT_PLAN"
else
  log "[dry-run] would write plan to: $OUT_PLAN"
fi

log "done"
