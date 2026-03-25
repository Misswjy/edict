# Rollback Window Data Reinjection Plan

- Backup directory: /tmp/edict-backups/20260325T084044Z
- Window start: 2026-03-25T02:44:58Z
- Window end: 2026-03-25T08:44:58Z
- Delta output: ops/cutover/v2_delta.latest.json
- Merged output: ops/cutover/tasks_source.merged.json
- Merge report: ops/cutover/reinject_merge_report.json

## Goal

Reinject tasks and key metadata that were created or changed in v2 during rollback window, back into legacy data files.

## Executed Commands

1. `python3 ops/cutover/export_v2_delta.py --from 2026-03-25T02:44:58Z --to 2026-03-25T08:44:58Z --tasks-file data/live_status.json --audits-file data/task_audit_log.json --output ops/cutover/v2_delta.latest.json`
2. `python3 ops/cutover/merge_delta_into_legacy.py --delta-file ops/cutover/v2_delta.latest.json --legacy-file data/tasks_source.json --output-file ops/cutover/tasks_source.merged.json --report-file ops/cutover/reinject_merge_report.json`

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

1. Review `ops/cutover/reinject_merge_report.json`.
2. Verify merged payload against parity checks.
3. Replace `data/tasks_source.json` only after manual approval.

## Historical Conflict Policy Reference

- Merge into `data/tasks_source.json` with conflict policy:
   - keep higher `_stateVersion`
   - keep latest `updatedAt`
   - append `flow_log` without duplicates
