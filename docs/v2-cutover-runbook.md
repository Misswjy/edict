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

## 4. Rollback-Window Data Reinjection (Placeholder)

Current scaffold provides a plan generator only:

```bash
bash ops/cutover/reinject_tasks_placeholder.sh --execute \
  --backup-dir ops/backups/<timestamp> \
  --from 2026-03-25T00:00:00Z \
  --to 2026-03-25T04:00:00Z \
  --out-plan ops/cutover/reinject_plan.md
```

The generated plan defines required TODOs:
- export v2 deltas by time window
- merge with deterministic conflict policy into legacy JSON
- run parity checks before reopening legacy writes

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

## 6. Notes

- All scripts default to dry-run mode.
- Apply environment-specific credentials and paths before production usage.
- This runbook intentionally avoids changing legacy/v2 application code paths.

