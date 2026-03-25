"""Shared durable audit persistence for control-plane and compatibility services."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from ..task_contract import ActorContext, build_audit_entry

log = logging.getLogger("edict.audit")


def _parse_iso(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


async def persist_task_audit_entry(entry: dict[str, Any]) -> dict[str, Any]:
    from ..db import async_session
    from ..models.task_audit import TaskAudit

    async with async_session() as db:
        db.add(
            TaskAudit(
                audit_id=uuid.UUID(str(entry["audit_id"])),
                ts=_parse_iso(entry.get("ts")) or datetime.now(timezone.utc),
                request_id=str(entry.get("request_id") or ""),
                task_id=str(entry.get("task_id") or ""),
                action=str(entry.get("action") or ""),
                actor_id=str(entry.get("actor_id") or ""),
                actor_type=str(entry.get("actor_type") or "agent"),
                source=str(entry.get("source") or "unknown"),
                signature_verified=bool(entry.get("signature_verified")),
                from_state=str(entry.get("from_state") or ""),
                to_state=str(entry.get("to_state") or ""),
                target_agent=str(entry.get("target_agent") or ""),
                allowed=bool(entry.get("allowed")),
                deny_reason=str(entry.get("deny_reason") or ""),
                policy_version=str(entry.get("policy_version") or ""),
                payload=dict(entry.get("payload") or {}),
                payload_hash=str(entry.get("payload_hash") or ""),
                payload_summary=str(entry.get("payload_summary") or ""),
            )
        )
        await db.commit()
    log.info("task_audit=%s", json.dumps(entry, ensure_ascii=False))
    return entry


async def record_task_audit(
    *,
    task_id: str,
    action: str,
    actor: ActorContext,
    allowed: bool,
    from_state: str = "",
    to_state: str = "",
    target_agent: str = "",
    deny_reason: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = build_audit_entry(
        task_id=task_id,
        action=action,
        actor=actor,
        allowed=allowed,
        from_state=from_state,
        to_state=to_state,
        target_agent=target_agent,
        deny_reason=deny_reason,
        payload=payload,
    )
    try:
        await persist_task_audit_entry(entry)
    except ModuleNotFoundError:
        log.warning("task audit persistence unavailable; dependency missing action=%s task_id=%s", action, task_id)
    except Exception:
        log.exception("task audit persistence failed action=%s task_id=%s", action, task_id)
    return entry
