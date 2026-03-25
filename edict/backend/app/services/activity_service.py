"""Unified activity/projection view helpers for task timelines."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ..event_contract import (
    TOPIC_AGENT_TODO_UPDATE,
    TOPIC_TASK_COMPLETED,
    TOPIC_TASK_CREATED,
    TOPIC_TASK_DISPATCH,
    TOPIC_TASK_STATUS,
)
from ..task_contract import AGENT_DIRECTORY, ensure_task_shape, resolve_dispatch_agent

AGENT_LABELS = {str(item.get("id") or ""): str(item.get("label") or "") for item in AGENT_DIRECTORY}

UI_TO_PROJECTION_TODO_STATUS = {
    "not-started": "open",
    "not_started": "open",
    "open": "open",
    "in-progress": "in_progress",
    "in_progress": "in_progress",
    "doing": "in_progress",
    "completed": "done",
    "complete": "done",
    "done": "done",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}
PROJECTION_TO_UI_TODO_STATUS = {
    "open": "not-started",
    "in_progress": "in-progress",
    "done": "completed",
    "cancelled": "completed",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _todo_projection_status(status: str | None) -> str:
    return UI_TO_PROJECTION_TODO_STATUS.get(str(status or "").strip().lower(), "open")


def _todo_ui_status(status: str | None) -> str:
    normalized = _todo_projection_status(status)
    return PROJECTION_TO_UI_TODO_STATUS.get(normalized, "not-started")


def build_todo_projection_rows(
    *,
    trace_id: str,
    items: list[dict[str, Any]] | None,
    producer: str,
    source_of_truth: str,
    projected_at: datetime | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    now = projected_at or datetime.now(timezone.utc)
    for index, raw_item in enumerate(items or []):
        if not isinstance(raw_item, dict):
            continue
        item_id = str(raw_item.get("id") or index + 1)
        rows.append(
            {
                "todo_id": uuid.uuid5(uuid.NAMESPACE_URL, f"edict:{trace_id}:{item_id}"),
                "trace_id": trace_id,
                "parent_id": None,
                "title": str(raw_item.get("title") or raw_item.get("name") or f"Todo {index + 1}"),
                "description": str(raw_item.get("detail") or raw_item.get("description") or ""),
                "owner": str(raw_item.get("owner") or ""),
                "assignee_agent": str(
                    raw_item.get("assigneeAgent")
                    or raw_item.get("assignee_agent")
                    or raw_item.get("agent")
                    or ""
                ),
                "status": _todo_projection_status(raw_item.get("status")),
                "priority": str(raw_item.get("priority") or "normal"),
                "estimated_cost": _safe_float(
                    raw_item.get("estimatedCost", raw_item.get("estimated_cost")),
                    0.0,
                ),
                "created_by": producer,
                "checkpoints": list(raw_item.get("checkpoints") or []),
                "metadata_": {
                    "order": index,
                    "legacyItemId": item_id,
                    "rawStatus": str(raw_item.get("status") or ""),
                    "source_of_truth": source_of_truth,
                    "raw": dict(raw_item),
                },
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def projected_todos_to_items(projected_rows: list[Any] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in sorted(
        projected_rows or [],
        key=lambda item: (
            int((item.metadata_ or {}).get("order") or 0),
            str(getattr(item, "created_at", "") or ""),
        ),
    ):
        raw = dict((row.metadata_ or {}).get("raw") or {})
        item_id = str(raw.get("id") or (row.metadata_ or {}).get("legacyItemId") or row.todo_id)
        items.append(
            {
                "id": item_id,
                "title": raw.get("title") or row.title,
                "status": _todo_ui_status(raw.get("status") or row.status),
                "detail": raw.get("detail") or raw.get("description") or row.description or "",
            }
        )
    return items


def compute_todos_summary(items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    current = list(items or [])
    if not current:
        return None
    total = len(current)
    completed = sum(1 for item in current if _todo_ui_status(item.get("status")) == "completed")
    in_progress = sum(1 for item in current if _todo_ui_status(item.get("status")) == "in-progress")
    not_started = max(0, total - completed - in_progress)
    percent = round(completed / total * 100) if total else 0
    return {
        "total": total,
        "completed": completed,
        "inProgress": in_progress,
        "notStarted": not_started,
        "percent": percent,
    }


def compute_todos_diff(
    previous_items: list[dict[str, Any]] | None,
    current_items: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    previous_map = {str(item.get("id") or ""): item for item in (previous_items or [])}
    current_map = {str(item.get("id") or ""): item for item in (current_items or [])}
    changed: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []

    for item_id, current in current_map.items():
        if item_id in previous_map:
            previous = previous_map[item_id]
            previous_status = _todo_ui_status(previous.get("status"))
            current_status = _todo_ui_status(current.get("status"))
            if previous_status != current_status:
                changed.append(
                    {
                        "id": item_id,
                        "title": current.get("title", ""),
                        "from": previous_status,
                        "to": current_status,
                    }
                )
        else:
            added.append({"id": item_id, "title": current.get("title", "")})

    for item_id, previous in previous_map.items():
        if item_id not in current_map:
            removed.append({"id": item_id, "title": previous.get("title", "")})

    if not changed and not added and not removed:
        return None
    return {"changed": changed, "added": added, "removed": removed}


def compute_phase_durations(flow_log: list[dict[str, Any]] | None, updated_at: str | None) -> list[dict[str, Any]]:
    entries = [item for item in (flow_log or []) if isinstance(item, dict) and item.get("at")]
    if not entries:
        return []
    phases: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        current_at = str(entry.get("at") or "")
        next_at = str(entries[index + 1].get("at") or "") if index + 1 < len(entries) else str(updated_at or current_at)
        try:
            start = datetime.fromisoformat(current_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(next_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        duration_sec = max(0, int((end - start).total_seconds()))
        hours = duration_sec // 3600
        minutes = (duration_sec % 3600) // 60
        seconds = duration_sec % 60
        if hours:
            duration_text = f"{hours}小时{minutes}分"
        elif minutes:
            duration_text = f"{minutes}分{seconds}秒"
        else:
            duration_text = f"{seconds}秒"
        phases.append(
            {
                "phase": str(entry.get("to") or ""),
                "from": current_at,
                "to": next_at,
                "durationSec": duration_sec,
                "durationText": duration_text,
                "ongoing": index == len(entries) - 1,
                "remark": str(entry.get("remark") or ""),
            }
        )
    return phases


def _extract_flow_entry_from_event(event: Any) -> dict[str, Any] | None:
    payload = dict(event.payload or {})
    task_payload = dict(payload.get("task") or {})
    flow_log = list(task_payload.get("flow_log") or task_payload.get("flowLog") or payload.get("flow_log") or [])
    if flow_log:
        latest = dict(flow_log[-1] or {})
        return {
            "at": latest.get("at") or (event.timestamp.isoformat() if event.timestamp else ""),
            "kind": "flow",
            "from": latest.get("from") or payload.get("from", ""),
            "to": latest.get("to") or payload.get("org", ""),
            "remark": latest.get("remark") or payload.get("reason", ""),
        }

    if event.topic == TOPIC_TASK_CREATED:
        return {
            "at": event.timestamp.isoformat() if event.timestamp else "",
            "kind": "flow",
            "from": "皇上",
            "to": payload.get("org") or task_payload.get("org") or "司礼监",
            "remark": f"下旨：{payload.get('title') or task_payload.get('title') or ''}",
        }
    if event.topic in {TOPIC_TASK_STATUS, TOPIC_TASK_COMPLETED}:
        return {
            "at": event.timestamp.isoformat() if event.timestamp else "",
            "kind": "flow",
            "from": payload.get("from", ""),
            "to": payload.get("to", ""),
            "remark": payload.get("reason", "") or f"状态推进到 {payload.get('to', '')}",
        }
    if event.topic == TOPIC_TASK_DISPATCH and event.event_type == "task.consult.request":
        return {
            "at": event.timestamp.isoformat() if event.timestamp else "",
            "kind": "flow",
            "from": event.producer,
            "to": payload.get("agent", ""),
            "remark": payload.get("message", "") or "横向咨询",
        }
    return None


def _thought_to_activity_entry(thought: Any) -> dict[str, Any]:
    thinking_types = {"reasoning", "query", "action_intent"}
    if str(thought.type or "").strip().lower() in thinking_types or str(thought.source or "").strip().lower() == "llm":
        return {
            "at": thought.timestamp.isoformat() if thought.timestamp else "",
            "kind": "assistant",
            "agent": thought.agent,
            "thinking": thought.content,
        }
    return {
        "at": thought.timestamp.isoformat() if thought.timestamp else "",
        "kind": "assistant",
        "agent": thought.agent,
        "text": thought.content,
    }


def build_task_activity_payload(
    task_snapshot: dict[str, Any],
    events: list[Any] | None,
    thoughts: list[Any] | None,
    projected_todos: list[Any] | None,
) -> dict[str, Any]:
    task_data = ensure_task_shape(dict(task_snapshot or {}))
    flow_entries: list[dict[str, Any]] = []
    progress_entries: list[dict[str, Any]] = []
    todo_entries: list[dict[str, Any]] = []
    assistant_entries: list[dict[str, Any]] = []
    related_agents: set[str] = set()
    seen_flow_keys: set[tuple[str, str, str, str]] = set()
    previous_todos: list[dict[str, Any]] | None = None

    total_tokens = 0
    total_cost = 0.0
    total_elapsed = 0

    for event in sorted(events or [], key=lambda item: item.timestamp or datetime.min.replace(tzinfo=timezone.utc)):
        if event.topic in {TOPIC_TASK_CREATED, TOPIC_TASK_STATUS, TOPIC_TASK_COMPLETED} or (
            event.topic == TOPIC_TASK_DISPATCH and event.event_type == "task.consult.request"
        ):
            flow = _extract_flow_entry_from_event(event)
            if flow:
                key = (
                    str(flow.get("at") or ""),
                    str(flow.get("from") or ""),
                    str(flow.get("to") or ""),
                    str(flow.get("remark") or ""),
                )
                if key not in seen_flow_keys:
                    seen_flow_keys.add(key)
                    flow_entries.append(flow)
        if event.topic == TOPIC_AGENT_TODO_UPDATE:
            payload = dict(event.payload or {})
            items = [
                {
                    "id": str(item.get("id") or index + 1),
                    "title": str(item.get("title") or item.get("name") or f"Todo {index + 1}"),
                    "status": _todo_ui_status(item.get("status")),
                    "detail": str(item.get("detail") or item.get("description") or ""),
                }
                for index, item in enumerate(payload.get("items") or [])
                if isinstance(item, dict)
            ]
            diff = compute_todos_diff(previous_todos, items)
            todo_entries.append(
                {
                    "at": event.timestamp.isoformat() if event.timestamp else "",
                    "kind": "todos",
                    "agent": event.producer,
                    "items": items,
                    "diff": diff,
                }
            )
            if event.producer:
                related_agents.add(event.producer)
            previous_todos = items

    for progress in list(task_data.get("progress_log") or []):
        if not isinstance(progress, dict):
            continue
        agent = str(progress.get("agent") or "")
        if agent:
            related_agents.add(agent)
        total_tokens += int(progress.get("tokens") or 0)
        total_cost += _safe_float(progress.get("cost"), 0.0)
        total_elapsed += int(progress.get("elapsed") or 0)
        progress_entries.append(
            {
                "at": progress.get("at"),
                "kind": "progress",
                "text": progress.get("text"),
                "agent": agent,
                "agentLabel": AGENT_LABELS.get(agent, ""),
                "state": progress.get("state"),
                "org": progress.get("org"),
            }
        )
        progress_todos = list(progress.get("todos") or [])
        if progress_todos:
            items = [
                {
                    "id": str(item.get("id") or index + 1),
                    "title": str(item.get("title") or item.get("name") or f"Todo {index + 1}"),
                    "status": _todo_ui_status(item.get("status")),
                    "detail": str(item.get("detail") or item.get("description") or ""),
                }
                for index, item in enumerate(progress_todos)
                if isinstance(item, dict)
            ]
            todo_entries.append(
                {
                    "at": progress.get("at"),
                    "kind": "todos",
                    "items": items,
                    "agent": agent,
                    "agentLabel": AGENT_LABELS.get(agent, ""),
                    "state": progress.get("state"),
                    "org": progress.get("org"),
                    "diff": compute_todos_diff(previous_todos, items),
                }
            )
            previous_todos = items

    for thought in sorted(thoughts or [], key=lambda item: item.timestamp or datetime.min.replace(tzinfo=timezone.utc)):
        assistant_entries.append(_thought_to_activity_entry(thought))
        if thought.agent:
            related_agents.add(thought.agent)
        total_tokens += int(thought.tokens or 0)

    if not flow_entries:
        for flow in list(task_data.get("flow_log") or []):
            if not isinstance(flow, dict):
                continue
            flow_entries.append(
                {
                    "at": flow.get("at"),
                    "kind": "flow",
                    "from": flow.get("from"),
                    "to": flow.get("to"),
                    "remark": flow.get("remark"),
                }
            )

    projected_items = projected_todos_to_items(projected_todos)
    if not todo_entries:
        current_items = projected_items or [
            {
                "id": str(item.get("id") or index + 1),
                "title": str(item.get("title") or item.get("name") or f"Todo {index + 1}"),
                "status": _todo_ui_status(item.get("status")),
                "detail": str(item.get("detail") or item.get("description") or ""),
            }
            for index, item in enumerate(task_data.get("todos") or [])
            if isinstance(item, dict)
        ]
        if current_items:
            todo_entries.append(
                {
                    "at": task_data.get("updatedAt"),
                    "kind": "todos",
                    "items": current_items,
                    "agent": resolve_dispatch_agent(task_data, task_data.get("state")),
                }
            )

    current_agent = resolve_dispatch_agent(task_data, task_data.get("state"))
    if current_agent:
        related_agents.add(current_agent)

    activity = sorted(
        [*flow_entries, *progress_entries, *todo_entries, *assistant_entries],
        key=lambda item: str(item.get("at") or ""),
    )

    timestamps = [str(item.get("at") or "") for item in activity if item.get("at")]
    resource_summary = {
        "totalTokens": total_tokens,
        "totalCost": round(total_cost, 6),
        "totalElapsedSec": total_elapsed,
    }
    latest_items = projected_items or (todo_entries[-1].get("items") if todo_entries else [])

    return {
        "ok": True,
        "taskId": task_data.get("id", ""),
        "taskMeta": {
            "title": task_data.get("title", ""),
            "state": task_data.get("state", ""),
            "org": task_data.get("org", ""),
            "output": task_data.get("output", ""),
            "block": task_data.get("block", "无"),
            "priority": task_data.get("priority", "normal"),
            "reviewRound": int(task_data.get("review_round") or 0),
            "archived": bool(task_data.get("archived")),
        },
        "agentId": current_agent,
        "agentLabel": AGENT_LABELS.get(current_agent, ""),
        "activity": activity,
        "activitySource": "snapshot+events+projections",
        "relatedAgents": sorted(agent for agent in related_agents if agent),
        "lastActive": max(timestamps) if timestamps else task_data.get("updatedAt", ""),
        "phaseDurations": compute_phase_durations(task_data.get("flow_log"), task_data.get("updatedAt")),
        "todosSummary": compute_todos_summary(list(latest_items or [])),
        "resourceSummary": resource_summary if any(resource_summary.values()) else None,
        "projectedThoughtCount": len(thoughts or []),
        "projectedTodoCount": len(projected_todos or []),
    }


async def project_todos_snapshot(
    db: Any,
    *,
    trace_id: str,
    items: list[dict[str, Any]] | None,
    producer: str,
    source_of_truth: str,
    projected_at: datetime | None = None,
) -> int:
    from sqlalchemy import delete

    from ..models.todo import Todo

    rows = build_todo_projection_rows(
        trace_id=trace_id,
        items=items,
        producer=producer,
        source_of_truth=source_of_truth,
        projected_at=projected_at,
    )
    await db.execute(delete(Todo).where(Todo.trace_id == trace_id))
    if rows:
        db.add_all([Todo(**row) for row in rows])
    await db.commit()
    return len(rows)


async def build_task_activity_view(db: Any, task_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    from ..models.event import Event
    from ..models.task import Task
    from ..models.thought import Thought
    from ..models.todo import Todo

    task = await db.get(Task, task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")

    event_result = await db.execute(
        select(Event).where(Event.trace_id == task_id).order_by(Event.timestamp.asc())
    )
    thought_result = await db.execute(
        select(Thought).where(Thought.trace_id == task_id).order_by(Thought.timestamp.asc())
    )
    todo_result = await db.execute(
        select(Todo).where(Todo.trace_id == task_id).order_by(Todo.updated_at.asc(), Todo.created_at.asc())
    )

    return build_task_activity_payload(
        task.to_dict(),
        list(event_result.scalars().all()),
        list(thought_result.scalars().all()),
        list(todo_result.scalars().all()),
    )
