# Cutover Rehearsal Evidence (2026-03-25)

## Backup rehearsal

- Command:
  `bash ops/backup/backup_all.sh --execute --output-root /tmp/edict-backups --data-dir data --pg-dsn postgresql://xingzhan@127.0.0.1:5432/edict_browser_regression --redis-url redis://127.0.0.1:6389/0 --legacy-image cft0808/sansheng-demo:latest`
- Backup directory:
  `/tmp/edict-backups/20260325T084044Z`
- Verified artifacts:
  - `data/data.tar.gz`
  - `postgres/edict.dump`
  - `redis/dump.rdb`
  - `redis/persistence_info.txt`
  - `metadata/compose.snapshot.yml`
  - `metadata/manifest.env`
  - `metadata/legacy.image.inspect.json`
- Legacy image digest:
  `cft0808/sansheng-demo@sha256:e5e5f1c1db158c01356f2a946626c90bc61e4b092044ec6344a1b1ff0222d453`

## Legacy runtime rehearsal

- Command:
  `docker compose up -d sansheng-demo`
- Result:
  `http://127.0.0.1:7891/healthz` returned `200`
- Cleanup:
  `docker compose down`

## Rollback rehearsal

- Command:
  `bash ops/cutover/rollback_to_legacy.sh --execute --backup-dir /tmp/edict-backups/20260325T084044Z --compose-file /tmp/edict-rollback-rehearsal/missing-compose.yml --frontend-env-file /tmp/edict-rollback-rehearsal/.env.production.local --routing-config-file /tmp/edict-rollback-rehearsal/frontend-default.conf --manifest-file /tmp/edict-rollback-rehearsal/stage-manifest.json --freeze-marker /tmp/edict-rollback-rehearsal/.legacy_write_frozen --legacy-api-url http://127.0.0.1:7891`
- Executed results:
  - Restored backup archive back into `data/`
  - Rendered stage-0 rollback manifest to `/tmp/edict-rollback-rehearsal/stage-manifest.json`
  - Wrote `VITE_API_URL=/api` to `/tmp/edict-rollback-rehearsal/.env.production.local`
  - Executed reinjection scaffolding and produced:
    - `ops/cutover/v2_delta.latest.json`
    - `ops/cutover/tasks_source.merged.json`
    - `ops/cutover/reinject_merge_report.json`
    - `ops/cutover/reinject_plan.md`
  - Legacy health check passed against `http://127.0.0.1:7891/healthz`
- Boundaries of this rehearsal:
  - Compose-managed v2 service stop was skipped because local rehearsal used a non-existent compose file placeholder
  - Frontend container restart in legacy proxy mode was also skipped for the same reason
  - Reinjection path executed, but current local snapshot exported `deltaTasksExported=0` (`tasksSnapshotTotal=0`, `changedTaskIds=19`), so no non-empty merge case was validated
