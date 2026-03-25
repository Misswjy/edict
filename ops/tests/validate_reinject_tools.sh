#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

python3 -m py_compile \
  "$ROOT_DIR/ops/cutover/export_v2_delta.py" \
  "$ROOT_DIR/ops/cutover/merge_delta_into_legacy.py"
echo "[ok] py_compile export/merge tools"

cat >"$TMP_DIR/live_status.json" <<'JSON'
{
  "tasks": [
    {
      "id": "JJC-ROLL-001",
      "title": "回灌测试任务",
      "state": "Doing",
      "_stateVersion": 3,
      "updatedAt": "2026-03-25T02:00:00+00:00",
      "flow_log": [{"from": "尚书省", "to": "工部"}],
      "progress_log": [{"ts": "2026-03-25T02:00:00+00:00", "text": "v2进度"}],
      "consultLog": [],
      "todos": [{"id": "t1", "title": "执行", "status": "in-progress"}],
      "_scheduler": {"retryCount": 1}
    },
    {
      "id": "JJC-ROLL-002",
      "title": "窗口外任务",
      "state": "Pending",
      "_stateVersion": 1,
      "updatedAt": "2026-03-20T01:00:00+00:00"
    }
  ]
}
JSON

cat >"$TMP_DIR/task_audit_log.json" <<'JSON'
[
  {
    "audit_id": "a1",
    "ts": "2026-03-25T02:10:00+00:00",
    "task_id": "JJC-ROLL-001",
    "action": "task.state.Doing"
  },
  {
    "audit_id": "a2",
    "ts": "2026-03-25T02:20:00+00:00",
    "task_id": "JJC-ROLL-003",
    "action": "task.created"
  }
]
JSON

cat >"$TMP_DIR/tasks_source.json" <<'JSON'
{
  "tasks": [
    {
      "id": "JJC-ROLL-001",
      "title": "旧任务快照",
      "state": "Pending",
      "_stateVersion": 2,
      "updatedAt": "2026-03-25T01:00:00+00:00",
      "flow_log": [{"from": "中书省", "to": "门下省"}],
      "progress_log": [{"ts": "2026-03-25T01:00:00+00:00", "text": "legacy进度"}],
      "consultLog": [],
      "todos": [{"id": "t0", "title": "旧todo", "status": "not-started"}],
      "_scheduler": {}
    }
  ]
}
JSON

python3 "$ROOT_DIR/ops/cutover/export_v2_delta.py" \
  --from 2026-03-25T00:00:00Z \
  --to 2026-03-25T04:00:00Z \
  --tasks-file "$TMP_DIR/live_status.json" \
  --audits-file "$TMP_DIR/task_audit_log.json" \
  --output "$TMP_DIR/delta.json" \
  --dry-run
echo "[ok] export dry-run"

python3 "$ROOT_DIR/ops/cutover/export_v2_delta.py" \
  --from 2026-03-25T00:00:00Z \
  --to 2026-03-25T04:00:00Z \
  --tasks-file "$TMP_DIR/live_status.json" \
  --audits-file "$TMP_DIR/task_audit_log.json" \
  --output "$TMP_DIR/delta.json"
echo "[ok] export write"

python3 "$ROOT_DIR/ops/cutover/merge_delta_into_legacy.py" \
  --delta-file "$TMP_DIR/delta.json" \
  --legacy-file "$TMP_DIR/tasks_source.json" \
  --output-file "$TMP_DIR/tasks_source.merged.json" \
  --report-file "$TMP_DIR/merge_report.json" \
  --dry-run
echo "[ok] merge dry-run"

python3 "$ROOT_DIR/ops/cutover/merge_delta_into_legacy.py" \
  --delta-file "$TMP_DIR/delta.json" \
  --legacy-file "$TMP_DIR/tasks_source.json" \
  --output-file "$TMP_DIR/tasks_source.merged.json" \
  --report-file "$TMP_DIR/merge_report.json"
echo "[ok] merge write"

python3 - <<'PY' "$TMP_DIR/tasks_source.merged.json" "$TMP_DIR/merge_report.json"
import json, sys
merged = json.load(open(sys.argv[1], encoding="utf-8"))
report = json.load(open(sys.argv[2], encoding="utf-8"))
tasks = merged.get("tasks") if isinstance(merged, dict) else merged
assert isinstance(tasks, list)
t1 = next(item for item in tasks if item.get("id") == "JJC-ROLL-001")
assert int(t1.get("_stateVersion") or 0) == 3
assert len(t1.get("flow_log") or []) == 2
assert len(t1.get("todos") or []) == 2
assert report.get("updated", 0) >= 1
print("[ok] merged payload policy checks")
PY

echo "[ok] validate_reinject_tools complete"
