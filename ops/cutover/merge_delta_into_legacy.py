#!/usr/bin/env python3
"""Merge v2 rollback-window delta into legacy tasks_source.json deterministically."""

from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LIST_FIELDS = ("flow_log", "progress_log", "consultLog", "todos")


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


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def _state_version(task: dict[str, Any]) -> int:
    try:
        return int(task.get("_stateVersion") or task.get("state_version") or 0)
    except (TypeError, ValueError):
        return 0


def _updated_at(task: dict[str, Any]) -> datetime:
    return (
        _parse_iso(task.get("updatedAt"))
        or _parse_iso(task.get("updated_at"))
        or _parse_iso(task.get("createdAt"))
        or _parse_iso(task.get("created_at"))
        or datetime(1970, 1, 1, tzinfo=timezone.utc)
    )


def _list_union(left: list[Any], right: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for item in (left or []) + (right or []):
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _merged_task(existing: dict[str, Any], incoming: dict[str, Any]) -> tuple[dict[str, Any], str]:
    ex_ver = _state_version(existing)
    in_ver = _state_version(incoming)
    ex_ts = _updated_at(existing)
    in_ts = _updated_at(incoming)

    incoming_wins = (in_ver > ex_ver) or (in_ver == ex_ver and in_ts >= ex_ts)
    winner = incoming if incoming_wins else existing
    loser = existing if incoming_wins else incoming

    merged = copy.deepcopy(winner)
    for field in LIST_FIELDS:
        merged[field] = _list_union(
            list(loser.get(field) or []),
            list(winner.get(field) or []),
        )

    merged["_stateVersion"] = max(ex_ver, in_ver)
    if in_ts >= ex_ts:
        merged["updatedAt"] = incoming.get("updatedAt") or incoming.get("updated_at") or in_ts.isoformat()
    else:
        merged["updatedAt"] = existing.get("updatedAt") or existing.get("updated_at") or ex_ts.isoformat()

    if not isinstance(merged.get("_scheduler"), dict):
        merged["_scheduler"] = {}
    return merged, ("updated_from_delta" if incoming_wins else "kept_legacy")


def _extract_tasks(payload: Any) -> tuple[list[dict[str, Any]], bool]:
    if isinstance(payload, dict):
        tasks = payload.get("tasks")
        if isinstance(tasks, list):
            return [item for item in tasks if isinstance(item, dict)], True
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], False
    return [], False


def merge_delta(
    *,
    delta_payload: dict[str, Any],
    legacy_payload: Any,
) -> tuple[Any, dict[str, Any]]:
    legacy_tasks, wrapped = _extract_tasks(legacy_payload)
    delta_tasks = [item for item in (delta_payload.get("tasks") or []) if isinstance(item, dict)]

    legacy_by_id: dict[str, dict[str, Any]] = {}
    for task in legacy_tasks:
        task_id = str(task.get("id") or "").strip()
        if task_id:
            legacy_by_id[task_id] = task

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "window": delta_payload.get("window") or {},
        "deltaTaskCount": len(delta_tasks),
        "legacyTaskCountBefore": len(legacy_by_id),
        "added": 0,
        "updated": 0,
        "kept": 0,
        "skipped": 0,
        "actions": [],
    }

    for task in sorted(delta_tasks, key=lambda item: str(item.get("id") or "")):
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            report["skipped"] += 1
            report["actions"].append({"taskId": "", "action": "skipped_missing_id"})
            continue

        if task_id not in legacy_by_id:
            legacy_by_id[task_id] = copy.deepcopy(task)
            report["added"] += 1
            report["actions"].append({"taskId": task_id, "action": "added_from_delta"})
            continue

        merged, reason = _merged_task(legacy_by_id[task_id], task)
        legacy_by_id[task_id] = merged
        if reason == "updated_from_delta":
            report["updated"] += 1
        else:
            report["kept"] += 1
        report["actions"].append({"taskId": task_id, "action": reason})

    merged_tasks = [legacy_by_id[key] for key in sorted(legacy_by_id)]
    report["legacyTaskCountAfter"] = len(merged_tasks)

    if wrapped:
        merged_payload = dict(legacy_payload if isinstance(legacy_payload, dict) else {})
        merged_payload["tasks"] = merged_tasks
    else:
        merged_payload = merged_tasks
    return merged_payload, report


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge exported v2 delta into legacy tasks_source.json")
    parser.add_argument("--delta-file", required=True, help="delta JSON path from export_v2_delta.py")
    parser.add_argument("--legacy-file", default="data/tasks_source.json", help="legacy tasks_source JSON path")
    parser.add_argument("--output-file", default="", help="merged output path (default: legacy-file)")
    parser.add_argument("--report-file", default="", help="merge report JSON path")
    parser.add_argument("--dry-run", action="store_true", help="analyze only, do not write files")
    args = parser.parse_args()

    delta_file = Path(args.delta_file)
    legacy_file = Path(args.legacy_file)
    if not delta_file.exists():
        raise SystemExit(f"delta file not found: {delta_file}")

    delta_payload = _read_json(delta_file, {})
    if not isinstance(delta_payload, dict):
        raise SystemExit("delta payload must be a JSON object")

    legacy_payload = _read_json(legacy_file, {"tasks": []})
    merged_payload, report = merge_delta(
        delta_payload=delta_payload,
        legacy_payload=legacy_payload,
    )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("dry-run: no file written")
        return 0

    output_file = Path(args.output_file) if args.output_file else legacy_file
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(merged_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"merged payload written: {output_file}")

    if args.report_file:
        report_file = Path(args.report_file)
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"merge report written: {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
