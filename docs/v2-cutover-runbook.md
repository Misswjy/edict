# V2 Staged Cutover and Rollback Runbook

This runbook is aligned with `P2-4` in `V2_FULL_MIGRATION_CHECKLIST.md`.
The target is staged migration (Stage 1-5), not one-shot full switch.

Current repository status:
- `ops/cutover/cutover_to_v2.sh` now exposes `--stage 1..5` and renders stage-aware frontend proxy routing.
- `ops/cutover/rollback_to_legacy.sh` renders a legacy-only proxy profile for rollback and observation fallback.
- `ops/cutover/rollback_to_legacy.sh --start-legacy` now starts the frozen legacy image/service from the root `docker-compose.yml`; the old source-based legacy runtime is no longer present in the repo.
- This document defines the required behavior contract and validation gate for each stage.

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

Do not enter Stage 1 unless the backup is complete and restorable.

## 2. Stage Behavior Contract (Stage 1-5)

| Stage | Read traffic | Write traffic | Worker / event consumption | Scheduler | Legacy observation |
| --- | --- | --- | --- | --- | --- |
| Stage 1 | Frontend read traffic goes to v2 | Manual control writes stay on legacy | Legacy path is still active | Legacy scheduler active | Baseline comparison starts |
| Stage 2 | Reads remain on v2 | Manual control writes move to v2 | Legacy path is still active | Legacy scheduler active | Monitor write parity and rollback signal |
| Stage 3 | Reads remain on v2 | Writes remain on v2 | Agent dispatch and event consumption move to v2 | Legacy scheduler active | Legacy becomes standby for worker rollback |
| Stage 4 | Reads remain on v2 | Writes remain on v2 | v2 worker/event active | Scheduler moves to v2 | Legacy scheduler disabled and observed |
| Stage 5 | All production traffic stays on v2 | v2 only | v2 only | v2 only | Legacy read-only observation window |

## 3. Stage 1: Frontend Read Traffic -> v2

Goal: switch read traffic only, keep write and execution paths unchanged.

Action template:
1. Execute `bash ops/cutover/cutover_to_v2.sh --execute --stage 1 --backup-dir ops/backups/<timestamp>`.
2. Frontend will pin `VITE_API_URL=/api` and render Stage 1 proxy rules: read paths -> v2, write paths -> legacy.
3. Keep legacy worker/event and scheduler paths running.

Validation gate:
- `curl -fsS http://127.0.0.1:8000/health`
- `python3 scripts/diff_legacy_vs_v2.py live-status --legacy-base-url http://127.0.0.1:7891 --v2-base-url http://127.0.0.1:8000`
- No non-whitelisted field diff on read-path core views.

Failure rollback action:
1. Re-render legacy routing with `bash ops/cutover/rollback_to_legacy.sh --backup-dir ops/backups/<timestamp>`.
2. Re-check legacy read endpoints.
3. Keep current write/worker/scheduler state unchanged.

## 4. Stage 2: Manual Control Write Traffic -> v2

Goal: move human-triggered control-plane writes to v2.

Action template:
1. Execute `bash ops/cutover/cutover_to_v2.sh --execute --stage 2 --backup-dir ops/backups/<timestamp>`.
2. Keep Stage 1 read routing unchanged and switch manual control write endpoints to v2.
3. Keep worker/event and scheduler on legacy for blast-radius control.

Validation gate:
- Execute representative write actions on canary tasks (create, review, advance, consult).
- `python3 scripts/diff_legacy_vs_v2.py action --endpoint task-action --body-file /tmp/task-action-body.json --legacy-base-url http://127.0.0.1:7891 --v2-base-url http://127.0.0.1:8000 --allow-mutation`
- No unexpected mismatch in action result contract.

Failure rollback action:
1. Route manual control writes back to legacy.
2. Keep reads on v2 only if Stage 1 checks still pass.
3. Record divergence samples for contract fix before retry.

## 5. Stage 3: Agent Dispatch and Event Consumption -> v2

Goal: move execution path (dispatch + event consume) to v2 workers.

Action template:
1. Execute `bash ops/cutover/cutover_to_v2.sh --execute --stage 3 --backup-dir ops/backups/<timestamp>`.
2. Keep read/write routing as in Stage 2 and enable v2 `orchestrator` / `dispatcher`.
3. Keep legacy scheduler/loop active until Stage 4 to reduce blast radius.

Validation gate:
- `python3 scripts/diff_legacy_vs_v2.py queue-metrics --legacy-base-url http://127.0.0.1:7891 --v2-base-url http://127.0.0.1:8000`
- Worker heartbeat stays stable and pending event backlog does not trend upward.
- Task state progression latency stays inside expected SLA.

Failure rollback action:
1. Stop v2 worker/event consumers.
2. Restore legacy dispatch/event consumers.
3. Keep Stage 2 write routing decision only if no data divergence is found.

## 6. Stage 4: Scheduler -> v2

Goal: move scheduling decisions to v2.

Action template:
1. Execute `bash ops/cutover/cutover_to_v2.sh --execute --stage 4 --backup-dir ops/backups/<timestamp>`.
2. Keep Stage 3 read/write/worker state unchanged and enable v2 `scheduler`.
3. Disable legacy scheduler/loop triggers.

Validation gate:
- Verify `scheduler-state` parity on sample tasks:

```bash
python3 scripts/diff_legacy_vs_v2.py scheduler-state \
  --task-id JJC-20260325-001 \
  --legacy-base-url http://127.0.0.1:7891 \
  --v2-base-url http://127.0.0.1:8000
```

- Retry/escalation/rollback counters remain in expected band.
- No sustained SLA miss after scheduler handoff.

Failure rollback action:
1. Stop v2 scheduler.
2. Re-enable legacy scheduler path.
3. Keep Stage 3 worker routing unless scheduler-side data corruption is detected.

## 7. Stage 5: Legacy Read-Only Observation Window

Goal: keep v2 as production path while legacy remains read-only safety window.

Action template:
1. Execute `bash ops/cutover/cutover_to_v2.sh --execute --stage 5 --backup-dir ops/backups/<timestamp>`.
2. Keep all read/write/worker/scheduler traffic on v2.
3. Enforce legacy no-write guard (`ops/cutover/.legacy_write_frozen` must exist).
4. Continue parity and drift observation for the defined window.

Validation gate:
- Continuous read parity checks remain stable.
- No unrecoverable drift between v2 and legacy snapshots.
- Rollback assets (backup + reinjection tooling) remain usable.

Failure rollback action:
1. Run rollback dry-run first:

```bash
bash ops/cutover/rollback_to_legacy.sh \
  --backup-dir ops/backups/<timestamp>
```

2. If dry-run output is acceptable, execute rollback.
3. Run reinjection scaffolding for rollback-window delta reconciliation.

## 8. Stage Commands

Recommended stage cutover commands:

```bash
bash ops/cutover/cutover_to_v2.sh --execute --stage 1 \
  --backup-dir ops/backups/<timestamp> \
  --v2-api-url http://localhost:8000
```

```bash
bash ops/cutover/cutover_to_v2.sh --execute --stage 2 \
  --backup-dir ops/backups/<timestamp> \
  --v2-api-url http://localhost:8000
```

```bash
bash ops/cutover/cutover_to_v2.sh --execute --stage 3 \
  --backup-dir ops/backups/<timestamp> \
  --v2-api-url http://localhost:8000
```

```bash
bash ops/cutover/cutover_to_v2.sh --execute --stage 4 \
  --backup-dir ops/backups/<timestamp> \
  --v2-api-url http://localhost:8000
```

```bash
bash ops/cutover/cutover_to_v2.sh --execute --stage 5 \
  --backup-dir ops/backups/<timestamp> \
  --v2-api-url http://localhost:8000
```

## 9. Monitoring & Alerting Gate

At every stage, run the monitoring checks tied to P1-16/P1-17 before trusting the cutover shift:
- `curl -fsS http://127.0.0.1:8000/api/admin/health/deep` – validates Postgres, Redis, worker heartbeats, stream groups, queue metrics, and scheduler thresholds (`thresholds`, `workers`, `streams` payload).
- `curl -fsS http://127.0.0.1:8000/api/metrics/prometheus` (or `curl -fsS http://127.0.0.1:8000/api/metrics/snapshot`) – ensures `edict_health_*`, `edict_worker_*`, `edict_stream_*`, `edict_stream_pending_total`, `edict_stream_lag_total`, `edict_stream_group_*`, and `edict_monitoring_up` are present and consistent with alert templates.
- `curl -fsS http://127.0.0.1:8000/api/queue-metrics` – spot-check task volume, state distribution, central queue backlog, and fast lane counts.
- `cat ops/alerts/prometheus-rules.example.yml` + `/api/metrics/prometheus` – confirm the alert file’s five rules (pending backlog, worker heartbeat loss, queue SLA breach, scheduler rollback/escalation burst, frontend websocket disconnect) align with the exported metric names/labels; see `ops/alerts/README.md` for rollout guidance.

Failure in these checks freezes staging writes and triggers rollback: stop the stage, rerun diagnostics, and only advance once all metrics/alerts are healthy.

Rollback command:

```bash
bash ops/cutover/rollback_to_legacy.sh --execute \
  --backup-dir ops/backups/<timestamp> \
  --start-legacy \
  --legacy-api-url http://127.0.0.1:7891
```

## 10. Rollback-Window Data Reinjection

Export rollback-window delta:

```bash
python3 ops/cutover/export_v2_delta.py \
  --from 2026-03-25T00:00:00Z \
  --to 2026-03-25T04:00:00Z \
  --tasks-file data/live_status.json \
  --audits-file data/task_audit_log.json \
  --output ops/cutover/v2_delta.sample.json \
  --dry-run
```

Merge into legacy source:

```bash
python3 ops/cutover/merge_delta_into_legacy.py \
  --delta-file ops/cutover/v2_delta.sample.json \
  --legacy-file data/tasks_source.json \
  --output-file ops/cutover/tasks_source.merged.sample.json \
  --report-file ops/cutover/reinject_merge_report.sample.json \
  --dry-run
```

Or use wrapper:

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

## 11. Alerts

Template rules:

```bash
cat ops/alerts/prometheus-rules.example.yml
```

Coverage:
- pending event backlog
- worker heartbeat missing
- central queue SLA backlog
- rollback/escalation burst
- frontend websocket disconnect spikes

## 12. Notes

- All scripts default to dry-run mode.
- Apply environment-specific credentials and paths before production usage.
- The frontend production build now fixes `VITE_API_URL=/api`; actual stage split happens in generated nginx routing.
