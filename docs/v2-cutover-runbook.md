# V2 Cutover and Rollback Runbook

This runbook covers migration gate scaffolding for:
- `P0-3` cutover and rollback process
- `P1-17` alert-template usage

## 1. Pre-Cutover Backup

Run dry-run first:

```bash
bash ops/backup/backup_all.sh
```

Run real backup:

```bash
bash ops/backup/backup_all.sh --execute \
  --legacy-image cft0808/sansheng-demo:latest
```

Expected backup outputs:
- `data/data.tar.gz`
- `postgres/edict.dump`
- `redis/dump.rdb`
- `metadata/manifest.env`
- optional legacy image metadata or tar

## 2. Cutover to v2

Dry-run:

```bash
bash ops/cutover/cutover_to_v2.sh \
  --backup-dir ops/backups/<timestamp>
```

Execute:

```bash
bash ops/cutover/cutover_to_v2.sh --execute \
  --backup-dir ops/backups/<timestamp> \
  --v2-api-url http://localhost:8000
```

What this script does:
1. Freeze legacy write traffic (marker + stop legacy loop/server process patterns).
2. Switch frontend API target to `VITE_API_URL=<v2-url>`.
3. Start v2 compose services (`backend`, `orchestrator`, `dispatcher`, `scheduler`, `frontend`).
4. Run basic health checks.

## 3. Rollback to legacy

Dry-run:

```bash
bash ops/cutover/rollback_to_legacy.sh \
  --backup-dir ops/backups/<timestamp>
```

Execute:

```bash
bash ops/cutover/rollback_to_legacy.sh --execute \
  --backup-dir ops/backups/<timestamp> \
  --legacy-api-url http://127.0.0.1:7891
```

Optional: auto-start legacy loop/server after rollback.

```bash
bash ops/cutover/rollback_to_legacy.sh --execute \
  --backup-dir ops/backups/<timestamp> \
  --start-legacy
```

## 4. Rollback-Window Data Reinjection

Reinjection scaffolding now has executable export+merge CLIs:

```bash
python3 ops/cutover/export_v2_delta.py \
  --from 2026-03-25T00:00:00Z \
  --to 2026-03-25T04:00:00Z \
  --tasks-file data/live_status.json \
  --audits-file data/task_audit_log.json \
  --output ops/cutover/v2_delta.sample.json \
  --dry-run
```

```bash
python3 ops/cutover/merge_delta_into_legacy.py \
  --delta-file ops/cutover/v2_delta.sample.json \
  --legacy-file data/tasks_source.json \
  --output-file ops/cutover/tasks_source.merged.sample.json \
  --report-file ops/cutover/reinject_merge_report.sample.json \
  --dry-run
```

Or run the wrapper script (recommended):

```bash
bash ops/cutover/reinject_tasks_placeholder.sh --execute \
  --backup-dir ops/backups/<timestamp> \
  --from 2026-03-25T00:00:00Z \
  --to 2026-03-25T04:00:00Z \
  --delta-out ops/cutover/v2_delta.window.json \
  --merged-out ops/cutover/tasks_source.merged.window.json \
  --merge-report ops/cutover/reinject_merge_report.window.json \
  --out-plan ops/cutover/reinject_plan.window.md
```

Deterministic merge policy:
- keep higher `_stateVersion`
- if tie, keep later `updatedAt`
- append `flow_log`/`progress_log`/`consultLog`/`todos` without duplicates

## 5. Alerts

Template rules:

```bash
cat ops/alerts/prometheus-rules.example.yml
```

Coverage included by template:
- pending event backlog
- worker heartbeat missing
- central queue SLA backlog
- rollback/escalation burst
- frontend websocket disconnect spikes

## 6. Parity Diff

Use the parity tool to compare legacy and v2 compatibility responses before or after cutover:

```bash
python3 scripts/diff_legacy_vs_v2.py live-status \
  --legacy-base-url http://127.0.0.1:7891 \
  --v2-base-url http://127.0.0.1:8000

python3 scripts/diff_legacy_vs_v2.py queue-metrics \
  --legacy-base-url http://127.0.0.1:7891 \
  --v2-base-url http://127.0.0.1:8000

python3 scripts/diff_legacy_vs_v2.py scheduler-state \
  --task-id JJC-20260325-001 \
  --legacy-base-url http://127.0.0.1:7891 \
  --v2-base-url http://127.0.0.1:8000
```

For action-result parity in isolated/shadow environments only:

```bash
python3 scripts/diff_legacy_vs_v2.py action \
  --endpoint task-action \
  --body-file /tmp/task-action-body.json \
  --legacy-base-url http://127.0.0.1:7891 \
  --v2-base-url http://127.0.0.1:8000 \
  --allow-mutation
```

## 7. Notes

- All scripts default to dry-run mode.
- Apply environment-specific credentials and paths before production usage.
- This runbook intentionally avoids changing legacy/v2 application code paths.
