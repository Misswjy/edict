#!/usr/bin/env bash
set -euo pipefail

EXECUTE=0
BACKUP_DIR="${BACKUP_DIR:-}"
FROM_TS="${FROM_TS:-}"
TO_TS="${TO_TS:-}"
OUT_PLAN="${OUT_PLAN:-ops/cutover/reinject_plan.md}"
DELTA_OUT="${DELTA_OUT:-ops/cutover/v2_delta.latest.json}"
MERGED_OUT="${MERGED_OUT:-ops/cutover/tasks_source.merged.json}"
MERGE_REPORT="${MERGE_REPORT:-ops/cutover/reinject_merge_report.json}"
TASKS_FILE="${TASKS_FILE:-data/live_status.json}"
AUDITS_FILE="${AUDITS_FILE:-data/task_audit_log.json}"
LEGACY_FILE="${LEGACY_FILE:-data/tasks_source.json}"

usage() {
  cat <<'EOF'
Usage: reinject_tasks_placeholder.sh [options]

This script now drives rollback-window reinjection scaffolding:
1) export delta from v2 snapshots
2) merge delta into legacy tasks_source with deterministic policy
3) generate an execution plan markdown as audit evidence

Options:
  --execute                     Perform real export+merge+plan write (default is dry-run)
  --backup-dir <dir>            Backup directory used for this rollback
  --from <timestamp>            Rollback window start (ISO8601)
  --to <timestamp>              Rollback window end (ISO8601)
  --tasks-file <file>           v2 task snapshot file (default: data/live_status.json)
  --audits-file <file>          v2 audit snapshot file (default: data/task_audit_log.json)
  --legacy-file <file>          legacy tasks_source file (default: data/tasks_source.json)
  --delta-out <file>            delta output json path
  --merged-out <file>           merged legacy output path
  --merge-report <file>         merge report output path
  --out-plan <file>             Output markdown plan path
  -h, --help                    Show this help
EOF
}

log() {
  printf '[reinject-placeholder] %s\n' "$*"
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
    --from)
      FROM_TS="${2:?missing value}"
      shift 2
      ;;
    --to)
      TO_TS="${2:?missing value}"
      shift 2
      ;;
    --tasks-file)
      TASKS_FILE="${2:?missing value}"
      shift 2
      ;;
    --audits-file)
      AUDITS_FILE="${2:?missing value}"
      shift 2
      ;;
    --legacy-file)
      LEGACY_FILE="${2:?missing value}"
      shift 2
      ;;
    --delta-out)
      DELTA_OUT="${2:?missing value}"
      shift 2
      ;;
    --merged-out)
      MERGED_OUT="${2:?missing value}"
      shift 2
      ;;
    --merge-report)
      MERGE_REPORT="${2:?missing value}"
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

if [[ -z "$FROM_TS" || -z "$TO_TS" ]]; then
  DEFAULT_WINDOW="$(python3 - <<'PY'
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc).replace(microsecond=0)
start = now - timedelta(hours=6)
print(start.isoformat().replace("+00:00", "Z"))
print(now.isoformat().replace("+00:00", "Z"))
PY
)"
  DEFAULT_FROM="$(printf '%s\n' "$DEFAULT_WINDOW" | sed -n '1p')"
  DEFAULT_TO="$(printf '%s\n' "$DEFAULT_WINDOW" | sed -n '2p')"
  if [[ -z "$FROM_TS" ]]; then
    FROM_TS="$DEFAULT_FROM"
  fi
  if [[ -z "$TO_TS" ]]; then
    TO_TS="$DEFAULT_TO"
  fi
fi

run_export() {
  local mode_flag="--dry-run"
  if [[ "$EXECUTE" -eq 1 ]]; then
    mode_flag=""
  fi
  if [[ -n "$mode_flag" ]]; then
    run_cmd python3 ops/cutover/export_v2_delta.py --from "$FROM_TS" --to "$TO_TS" --tasks-file "$TASKS_FILE" --audits-file "$AUDITS_FILE" --output "$DELTA_OUT" "$mode_flag"
  else
    run_cmd python3 ops/cutover/export_v2_delta.py --from "$FROM_TS" --to "$TO_TS" --tasks-file "$TASKS_FILE" --audits-file "$AUDITS_FILE" --output "$DELTA_OUT"
  fi
}

run_merge() {
  local mode_flag="--dry-run"
  if [[ "$EXECUTE" -eq 1 ]]; then
    mode_flag=""
  fi
  if [[ -n "$mode_flag" ]]; then
    run_cmd python3 ops/cutover/merge_delta_into_legacy.py --delta-file "$DELTA_OUT" --legacy-file "$LEGACY_FILE" --output-file "$MERGED_OUT" --report-file "$MERGE_REPORT" "$mode_flag"
  else
    run_cmd python3 ops/cutover/merge_delta_into_legacy.py --delta-file "$DELTA_OUT" --legacy-file "$LEGACY_FILE" --output-file "$MERGED_OUT" --report-file "$MERGE_REPORT"
  fi
}

PLAN_TEMPLATE=$(cat <<'EOF'
# Rollback Window Data Reinjection Plan

- Backup directory: __BACKUP_DIR__
- Window start: __FROM_TS__
- Window end: __TO_TS__
- Delta output: __DELTA_OUT__
- Merged output: __MERGED_OUT__
- Merge report: __MERGE_REPORT__

## Goal

Reinject tasks and key metadata that were created or changed in v2 during rollback window, back into legacy data files.

## Executed Commands

1. `python3 ops/cutover/export_v2_delta.py --from __FROM_TS__ --to __TO_TS__ --tasks-file __TASKS_FILE__ --audits-file __AUDITS_FILE__ --output __DELTA_OUT__`
2. `python3 ops/cutover/merge_delta_into_legacy.py --delta-file __DELTA_OUT__ --legacy-file __LEGACY_FILE__ --output-file __MERGED_OUT__ --report-file __MERGE_REPORT__`

## Deterministic Merge Policy

- key by `task.id`
- keep higher `_stateVersion`
- if `_stateVersion` tie, keep later `updatedAt`
- append logs without duplicates for:
  - `flow_log`
  - `progress_log`
  - `consultLog`
  - `todos`

## Next Steps

1. Review `__MERGE_REPORT__`.
2. Verify merged payload against parity checks.
3. Replace `data/tasks_source.json` only after manual approval.

## Historical Conflict Policy Reference

- Merge into `data/tasks_source.json` with conflict policy:
   - keep higher `_stateVersion`
   - keep latest `updatedAt`
   - append `flow_log` without duplicates
EOF
)

PLAN_CONTENT="${PLAN_TEMPLATE//__BACKUP_DIR__/${BACKUP_DIR:-TODO_SET_BACKUP_DIR}}"
PLAN_CONTENT="${PLAN_CONTENT//__FROM_TS__/$FROM_TS}"
PLAN_CONTENT="${PLAN_CONTENT//__TO_TS__/$TO_TS}"
PLAN_CONTENT="${PLAN_CONTENT//__TASKS_FILE__/$TASKS_FILE}"
PLAN_CONTENT="${PLAN_CONTENT//__AUDITS_FILE__/$AUDITS_FILE}"
PLAN_CONTENT="${PLAN_CONTENT//__LEGACY_FILE__/$LEGACY_FILE}"
PLAN_CONTENT="${PLAN_CONTENT//__DELTA_OUT__/$DELTA_OUT}"
PLAN_CONTENT="${PLAN_CONTENT//__MERGED_OUT__/$MERGED_OUT}"
PLAN_CONTENT="${PLAN_CONTENT//__MERGE_REPORT__/$MERGE_REPORT}"

run_export
run_merge

if [[ "$EXECUTE" -eq 1 ]]; then
  mkdir -p "$(dirname "$OUT_PLAN")"
  printf '%s\n' "$PLAN_CONTENT" > "$OUT_PLAN"
  log "plan written: $OUT_PLAN"
else
  log "[dry-run] would write plan to: $OUT_PLAN"
fi

log "done"
