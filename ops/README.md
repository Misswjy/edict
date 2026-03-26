# Ops Runbooks and Scripts

This directory contains migration-operation scaffolding for:
- `P0-3` cutover and rollback gates
- `P1-17` alert templates
- `P2-4` staged cutover documentation and validation hooks

Defaults are non-destructive:
- Most scripts run in dry-run mode by default.
- Pass `--execute` to perform real actions.

## Layout

- `ops/backup/backup_all.sh`: capture `data/`, Postgres dump, Redis snapshot, and legacy release metadata.
- `ops/restore/restore_all.sh`: restore snapshots from a backup directory.
- `ops/cutover/cutover_to_v2.sh`: freeze legacy writes, switch frontend API target, and enable v2 scheduler/worker chain.
- `ops/cutover/rollback_to_legacy.sh`: freeze v2 writes, restore data snapshot, switch traffic back to legacy runtime, and optionally start the frozen legacy image/service.
- `ops/cutover/export_v2_delta.py`: export rollback-window delta from v2 task/audit snapshots.
- `ops/cutover/merge_delta_into_legacy.py`: deterministically merge delta into legacy `tasks_source.json`.
- `ops/cutover/reinject_tasks_placeholder.sh`: orchestrate export+merge and emit reinjection plan evidence.
- `ops/alerts/prometheus-rules.example.yml`: baseline alert rules template.
- `ops/alerts/README.md`: alert metric mapping and rollout notes.
- `ops/tests/validate_ops_syntax.sh`: shell/python syntax checks plus runbook consistency checks.
- `ops/tests/validate_cutover_docs.sh`: staged cutover document keyword checks (Stage 1-5).

## P2-4 Staged Cutover Model (Stage 1-5)

The target migration model follows five stages from `V2_FULL_MIGRATION_CHECKLIST.md`.
Current cutover scripts render stage-aware frontend proxy config and converge compose services for each stage.

| Stage | Read traffic | Write traffic | Worker / event consumption | Scheduler | Legacy observation |
| --- | --- | --- | --- | --- | --- |
| Stage 1 | Frontend reads from v2 API | Keep manual control writes on legacy | Keep legacy worker/event as active path | Keep legacy scheduler active | Start baseline monitoring on legacy behavior |
| Stage 2 | Read stays on v2 | Switch manual control writes to v2 | Keep legacy worker/event as active path | Keep legacy scheduler active | Monitor write-side parity and rollback markers |
| Stage 3 | Read stays on v2 | Writes stay on v2 | Switch agent dispatch + event consumer to v2 | Keep legacy scheduler active | Legacy remains standby for process-level fallback |
| Stage 4 | Read stays on v2 | Writes stay on v2 | v2 worker/event stays active | Switch scheduler to v2 | Legacy scheduler enters standby only |
| Stage 5 | Read/write/worker/scheduler all on v2 | v2 only | v2 only | v2 only | Legacy enters read-only observation window |

## Recommended Execution Order

1. Backup first:

```bash
bash ops/backup/backup_all.sh --execute
```

2. Execute Stage 1-5 progressively using [`docs/v2-cutover-runbook.md`](../docs/v2-cutover-runbook.md) gates.
3. Stage cutover commands:

```bash
bash ops/cutover/cutover_to_v2.sh --execute --stage 1 \
  --backup-dir ops/backups/<timestamp> \
  --v2-api-url http://localhost:8000
```

```bash
bash ops/cutover/cutover_to_v2.sh --execute --stage 5 \
  --backup-dir ops/backups/<timestamp> \
  --v2-api-url http://localhost:8000
```

4. Keep rollback assets ready:

```bash
bash ops/cutover/rollback_to_legacy.sh --execute \
  --backup-dir ops/backups/<timestamp> \
  --start-legacy \
  --legacy-api-url http://127.0.0.1:7891
```

## Stage Verification and Rollback Triggers

Recommended checks after each stage:

- `curl -fsS http://127.0.0.1:8000/health`
- `python3 scripts/diff_legacy_vs_v2.py live-status --legacy-base-url http://127.0.0.1:7891 --v2-base-url http://127.0.0.1:8000`
- `python3 scripts/diff_legacy_vs_v2.py queue-metrics --legacy-base-url http://127.0.0.1:7891 --v2-base-url http://127.0.0.1:8000`

Rollback trigger points:

- Stage 1: read-side parity mismatch cannot be whitelisted.
- Stage 2: manual write action result diverges between legacy and v2.
- Stage 3: dispatch/event backlog grows continuously or worker heartbeat is unstable.
- Stage 4: scheduler SLA misses, retry/escalation bursts exceed expected bounds.
- Stage 5: observation window finds data drift that cannot be reconciled by delta reinjection.

## Monitoring & Alerts

Before moving to the next stage, exercise the P1-16/P1-17 monitoring gate:

- `curl -fsS http://127.0.0.1:8000/api/admin/health/deep` (Postgres, Redis, worker heartbeat, stream group, queue, scheduler thresholds).
- `curl -fsS http://127.0.0.1:8000/api/metrics/prometheus` (or `/api/metrics/snapshot`) to ensure `edict_health_*`, `edict_worker_*`, `edict_stream_*`, `edict_stream_pending_total`, `edict_stream_lag_total`, `edict_stream_group_*`, and `edict_monitoring_up` exist and reflect the stage.
- `curl -fsS http://127.0.0.1:8000/api/queue-metrics` for task volume, state distribution, central queue waiting/overdue counts and fast lane summary.
- `cat ops/alerts/prometheus-rules.example.yml` + `/api/metrics/prometheus` confirms the sample alert template covering pending backlog, worker heartbeat loss, central queue SLA, scheduler rollback/escalation bursts, and frontend WebSocket disconnect matches the exported metrics; see `ops/alerts/README.md` for rollout guidance and metric mappings.

Any failure of these checks freezes the stage writes and is treated as a rollback trigger; rerun the diagnostics after correcting the issue.

Rollback command (dry-run first):

```bash
bash ops/cutover/rollback_to_legacy.sh \
  --backup-dir ops/backups/<timestamp>
```
