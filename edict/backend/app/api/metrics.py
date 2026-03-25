"""Metrics API — Prometheus and JSON monitoring snapshots."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .admin import (
    DEFAULT_LAG_WARN,
    DEFAULT_PENDING_WARN,
    collect_runtime_monitoring,
)
from .auth import require_control_plane_access
from ..config import get_settings
from ..db import get_db
from ..services.event_bus import get_event_bus

router = APIRouter(dependencies=[Depends(require_control_plane_access)])


def _metric_line(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    if not labels:
        return f"{name} {value}"
    parts = ",".join(f'{key}="{str(val).replace(chr(34), chr(39))}"' for key, val in sorted(labels.items()))
    return f"{name}{{{parts}}} {value}"


async def _build_snapshot(db: AsyncSession) -> dict[str, Any]:
    settings = get_settings()
    stale_after_sec = max(60, int(getattr(settings, "heartbeat_interval_sec", 30) or 30) * 3)
    snapshot: dict[str, Any] = {
        "postgres": {"ok": False},
        "redis": {"ok": False},
        "workers": {"ok": False, "items": []},
        "streams": {"ok": False, "groups": [], "totals": {"pending": 0, "lag": 0, "missingGroups": 0, "unhealthyGroups": 0}},
        "thresholds": {
            "workerStaleSec": stale_after_sec,
            "pendingWarn": DEFAULT_PENDING_WARN,
            "lagWarn": DEFAULT_LAG_WARN,
        },
    }
    try:
        result = await db.execute(text("SELECT 1"))
        snapshot["postgres"]["ok"] = result.scalar() == 1
    except Exception as exc:  # pragma: no cover - runtime defensive guard
        snapshot["postgres"]["error"] = str(exc)

    try:
        bus = await get_event_bus()
        snapshot["redis"]["ok"] = (await bus.redis.ping()) is True
        if snapshot["redis"]["ok"]:
            runtime = await collect_runtime_monitoring(
                bus,
                stale_after_sec=stale_after_sec,
                pending_warn=DEFAULT_PENDING_WARN,
                lag_warn=DEFAULT_LAG_WARN,
            )
            snapshot["workers"] = runtime["workers"]
            snapshot["streams"] = runtime["streams"]
    except Exception as exc:  # pragma: no cover - runtime defensive guard
        snapshot["redis"]["error"] = str(exc)

    snapshot["ok"] = (
        bool(snapshot["postgres"].get("ok"))
        and bool(snapshot["redis"].get("ok"))
        and bool(snapshot["workers"].get("ok"))
        and bool(snapshot["streams"].get("ok"))
    )
    return snapshot


def _render_prometheus(snapshot: dict[str, Any]) -> str:
    lines: list[str] = [
        "# HELP edict_health_postgres_up Postgres connectivity check (1=up,0=down).",
        "# TYPE edict_health_postgres_up gauge",
        _metric_line("edict_health_postgres_up", 1 if snapshot["postgres"].get("ok") else 0),
        "# HELP edict_health_redis_up Redis connectivity check (1=up,0=down).",
        "# TYPE edict_health_redis_up gauge",
        _metric_line("edict_health_redis_up", 1 if snapshot["redis"].get("ok") else 0),
        "# HELP edict_worker_up Worker heartbeat status (1=healthy,0=stale/missing).",
        "# TYPE edict_worker_up gauge",
    ]

    for item in snapshot["workers"].get("items", []):
        labels = {
            "worker": str(item.get("worker") or "unknown"),
            "instance": str(item.get("instance_id") or "missing"),
            "status": str(item.get("status") or "unknown"),
        }
        lines.append(_metric_line("edict_worker_up", 1 if item.get("healthy") else 0, labels))
        age = item.get("ageSec")
        if age is not None:
            lines.append(_metric_line("edict_worker_heartbeat_age_seconds", float(age), labels))

    lines.extend(
        [
            "# HELP edict_stream_group_pending Pending messages per stream group.",
            "# TYPE edict_stream_group_pending gauge",
            "# HELP edict_stream_group_lag Consumer lag per stream group.",
            "# TYPE edict_stream_group_lag gauge",
            "# HELP edict_stream_group_consumers Consumer count per stream group.",
            "# TYPE edict_stream_group_consumers gauge",
        ]
    )
    for group in snapshot["streams"].get("groups", []):
        labels = {
            "topic": str(group.get("topic") or ""),
            "group": str(group.get("group") or ""),
        }
        lines.append(_metric_line("edict_stream_group_pending", float(group.get("pending") or 0), labels))
        lines.append(_metric_line("edict_stream_group_lag", float(group.get("lag") or 0), labels))
        lines.append(_metric_line("edict_stream_group_consumers", float(group.get("consumers") or 0), labels))

    totals = snapshot["streams"].get("totals", {})
    lines.extend(
        [
            "# HELP edict_stream_pending_total Total pending messages in monitored groups.",
            "# TYPE edict_stream_pending_total gauge",
            _metric_line("edict_stream_pending_total", float(totals.get("pending") or 0)),
            "# HELP edict_stream_lag_total Total lag in monitored groups.",
            "# TYPE edict_stream_lag_total gauge",
            _metric_line("edict_stream_lag_total", float(totals.get("lag") or 0)),
            "# HELP edict_monitoring_up Overall deep monitoring status (1=healthy,0=degraded).",
            "# TYPE edict_monitoring_up gauge",
            _metric_line("edict_monitoring_up", 1 if snapshot.get("ok") else 0),
        ]
    )
    return "\n".join(lines) + "\n"


@router.get("/snapshot")
async def metrics_snapshot(db: AsyncSession = Depends(get_db)):
    return await _build_snapshot(db)


@router.get("/prometheus", response_class=PlainTextResponse)
async def metrics_prometheus(db: AsyncSession = Depends(get_db)):
    snapshot = await _build_snapshot(db)
    return PlainTextResponse(_render_prometheus(snapshot), media_type="text/plain; version=0.0.4; charset=utf-8")

