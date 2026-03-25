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

## Local full rollback rehearsal

- Pre-step:
  created rollback-window seed task `JJC-20260325-003` on v2 and captured `/tmp/edict-rollback-window-live-status.json`
- Frontend build:
  `VITE_API_URL=http://127.0.0.1:7891 npm --prefix edict/frontend run build -- --outDir /tmp/edict-legacy-frontend-dist`
- Rollback command:
  `bash ops/cutover/rollback_to_legacy.sh --execute --skip-compose --backup-dir /tmp/edict-backups/20260325T084044Z --legacy-api-url http://127.0.0.1:7891 --freeze-v2-cmd "kill 85039" --frontend-switch-cmd "pkill -f 'http.server 5173' || true; nohup python3 -m http.server 5173 --directory /tmp/edict-legacy-frontend-dist >/tmp/edict-legacy-frontend.log 2>&1 &" --start-legacy --legacy-loop-cmd "EDICT_DISABLE_HTTP_SCHEDULER_SCAN=1 EDICT_LEGACY_LOOP_LOG=/tmp/edict-legacy-loop.rehearsal.log bash scripts/run_loop.sh 5 999" --legacy-server-cmd "python3 dashboard/server.py"`
- Runtime evidence:
  - v2 backend port `8000` was closed after rollback freeze
  - legacy server restored on `http://127.0.0.1:7891`
  - legacy-targeted frontend restored on `http://127.0.0.1:5173`
  - `scripts/run_loop.sh` kept refreshing snapshots; evidence in `/tmp/edict-legacy-loop.rehearsal.log`
- Browser validation:
  - `ops/tests/browser_regression_v2.py --frontend-url http://127.0.0.1:5173 --api-url http://127.0.0.1:7891 --health-url http://127.0.0.1:7891/healthz --output-dir ops/artifacts/browser-regression-legacy-rollback`
  - passed with screenshots in `ops/artifacts/browser-regression-legacy-rollback/`
- Non-empty reinjection validation:
  - `bash ops/cutover/reinject_tasks_placeholder.sh --execute --backup-dir /tmp/edict-backups/20260325T084044Z --from 2026-03-25T08:58:47Z --to 2026-03-25T09:08:47Z --tasks-file /tmp/edict-rollback-window-live-status.json --audits-file data/task_audit_log.json --legacy-file data/tasks_source.json --delta-out /tmp/edict-reinject-window/v2_delta.json --merged-out /tmp/edict-reinject-window/tasks_source.merged.json --merge-report /tmp/edict-reinject-window/reinject_merge_report.json --out-plan /tmp/edict-reinject-window/reinject_plan.md`
  - merge report result: `added=1`, `taskId=JJC-20260325-003`, `action=added_from_delta`

## Stage 5 observation window

- Worker recovery:
  started local `orchestrator` / `dispatcher` / `scheduler` workers against `edict_browser_regression`, bringing `/api/admin/health/deep` from `degraded` to `ok`
- Observation command:
  `python3 ops/tests/observe_stage5_window.py --backend-url http://127.0.0.1:8000 --frontend-url http://127.0.0.1:5173 --legacy-url http://127.0.0.1:7891/healthz --duration-sec 120 --interval-sec 15 --output ops/artifacts/stage5-observation-window/summary.json`
- Result:
  - window: `2026-03-25T09:29:47Z` -> `2026-03-25T09:31:48Z`
  - `sampleCount=9`
  - `failedSampleCount=0`
  - every sample kept:
    - backend `/health` = `ok`
    - deep health = `ok`
    - worker health = `ok`
    - queue metrics = `ok`
    - frontend `5173` = `200`
    - legacy `7891` remained closed
  - canary task `JJC-20260325-004` stayed visible through the whole window
