# Ops Runbooks and Scripts

This directory contains migration-operation scaffolding for:
- `P0-3` cutover and rollback gates
- `P1-17` alert templates

Defaults are non-destructive:
- Most scripts run in dry-run mode by default.
- Pass `--execute` to perform real actions.

## Layout

- `ops/backup/backup_all.sh`: capture `data/`, Postgres dump, Redis snapshot, and legacy release metadata.
- `ops/restore/restore_all.sh`: restore snapshots from a backup directory.
- `ops/cutover/cutover_to_v2.sh`: freeze legacy writes, switch frontend API target, and enable v2 scheduler/worker chain.
- `ops/cutover/rollback_to_legacy.sh`: freeze v2 writes, restore data snapshot, and switch traffic back to legacy runtime.
- `ops/cutover/reinject_tasks_placeholder.sh`: rollback-window data reinjection placeholder workflow.
- `ops/alerts/prometheus-rules.example.yml`: baseline alert rules template.
- `ops/alerts/README.md`: alert metric mapping and rollout notes.

## Quick Start

```bash
# 1) Create backup (dry-run)
bash ops/backup/backup_all.sh

# 2) Create backup (execute)
bash ops/backup/backup_all.sh --execute

# 3) Cutover to v2 (dry-run)
bash ops/cutover/cutover_to_v2.sh --backup-dir ops/backups/<timestamp>

# 4) Rollback to legacy (dry-run)
bash ops/cutover/rollback_to_legacy.sh --backup-dir ops/backups/<timestamp>
```

