#!/usr/bin/env python3
"""Export rollback-window delta from v2 task/audit snapshots.

Primary sources (file-based to stay portable in ops environments):
- tasks snapshot: data/live_status.json (or a task list JSON)
- audit snapshot: data/task_audit_log.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_iso(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_tasks(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if isinstance(payload, dict):
        data = payload.get("tasks")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _load_audits(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
        return [item for item in payload["entries"] if isinstance(item, dict)]
    return []


def _task_ts(task: dict[str, Any]) -> datetime | None:
    return (
        _parse_iso(task.get("updatedAt"))
        or _parse_iso(task.get("updated_at"))
        or _parse_iso(task.get("createdAt"))
        or _parse_iso(task.get("created_at"))
    )


def _audit_ts(entry: dict[str, Any]) -> datetime | None:
    return _parse_iso(entry.get("ts")) or _parse_iso(entry.get("timestamp"))


def _between(ts: datetime | None, start: datetime, end: datetime) -> bool:
    return bool(ts and start <= ts <= end)


def export_delta(
    *,
    tasks_file: Path,
    audits_file: Path,
    start: datetime,
    end: datetime,
    task_ids_filter: set[str] | None = None,
) -> dict[str, Any]:
    tasks = _load_tasks(tasks_file)
    audits = _load_audits(audits_file)

    task_map: dict[str, dict[str, Any]] = {}
    for task in tasks:
        task_id = str(task.get("id") or "").strip()
        if task_id:
            task_map[task_id] = task

    changed_task_ids: set[str] = set()
    tasks_in_window = 0
    for task_id, task in task_map.items():
        if _between(_task_ts(task), start, end):
            changed_task_ids.add(task_id)
            tasks_in_window += 1

    audits_in_window: list[dict[str, Any]] = []
    for entry in audits:
        if not _between(_audit_ts(entry), start, end):
            continue
        audits_in_window.append(entry)
        audit_task_id = str(entry.get("task_id") or "").strip()
        if audit_task_id:
            changed_task_ids.add(audit_task_id)

    if task_ids_filter:
        changed_task_ids = {task_id for task_id in changed_task_ids if task_id in task_ids_filter}
        audits_in_window = [
            entry
            for entry in audits_in_window
            if not str(entry.get("task_id") or "").strip()
            or str(entry.get("task_id") or "").strip() in task_ids_filter
        ]

    delta_tasks: list[dict[str, Any]] = []
    missing_task_ids: list[str] = []
    for task_id in sorted(changed_task_ids):
        snapshot = task_map.get(task_id)
        if snapshot is None:
            missing_task_ids.append(task_id)
            continue
        delta_tasks.append(snapshot)

    stats = {
        "tasksSnapshotTotal": len(task_map),
        "tasksChangedByTimestamp": tasks_in_window,
        "auditsTotal": len(audits),
        "auditsInWindow": len(audits_in_window),
        "changedTaskIds": len(changed_task_ids),
        "deltaTasksExported": len(delta_tasks),
        "missingTaskSnapshots": len(missing_task_ids),
    }

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "source": {"tasksFile": str(tasks_file), "auditsFile": str(audits_file)},
        "taskIds": sorted(changed_task_ids),
        "tasks": delta_tasks,
        "audits": audits_in_window,
        "missingTaskSnapshots": missing_task_ids,
        "stats": stats,
    }


def _default_output(start: datetime, end: datetime) -> Path:
    start_mark = start.strftime("%Y%m%dT%H%M%SZ")
    end_mark = end.strftime("%Y%m%dT%H%M%SZ")
    return Path("ops/cutover") / f"v2_delta_{start_mark}_{end_mark}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export v2 rollback-window delta from task/audit snapshots")
    parser.add_argument("--from", dest="from_ts", required=True, help="window start (ISO8601)")
    parser.add_argument("--to", dest="to_ts", required=True, help="window end (ISO8601)")
    parser.add_argument("--tasks-file", default="data/live_status.json", help="tasks snapshot JSON path")
    parser.add_argument("--audits-file", default="data/task_audit_log.json", help="task audit JSON path")
    parser.add_argument("--task-id", action="append", default=[], help="optional task id filter (repeatable)")
    parser.add_argument("--output", default="", help="delta output JSON path")
    parser.add_argument("--dry-run", action="store_true", help="analyze and print report, do not write file")
    args = parser.parse_args()

    start = _parse_iso(args.from_ts)
    end = _parse_iso(args.to_ts)
    if start is None or end is None:
        raise SystemExit("--from / --to must be valid ISO8601 timestamps")
    if start > end:
        raise SystemExit("--from must be <= --to")

    tasks_file = Path(args.tasks_file)
    audits_file = Path(args.audits_file)
    if not tasks_file.exists():
        raise SystemExit(f"tasks file not found: {tasks_file}")
    if not audits_file.exists():
        raise SystemExit(f"audits file not found: {audits_file}")

    task_filter = {item.strip() for item in args.task_id if item.strip()} or None
    payload = export_delta(
        tasks_file=tasks_file,
        audits_file=audits_file,
        start=start,
        end=end,
        task_ids_filter=task_filter,
    )

    print(json.dumps({"window": payload["window"], "stats": payload["stats"]}, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("dry-run: no file written")
        return 0

    output = Path(args.output) if args.output else _default_output(start, end)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"delta written: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
