"""Admin API — 管理操作（迁移、诊断、配置）。"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import require_control_plane_access
from ..config import get_settings
from ..db import get_db
from ..event_contract import TOPIC_TASK_DISPATCH
from ..services.event_bus import get_event_bus
from ..workers.orchestrator_worker import WATCHED_TOPICS

log = logging.getLogger("edict.api.admin")
router = APIRouter(dependencies=[Depends(require_control_plane_access)])

REQUIRED_WORKERS = ("orchestrator", "dispatcher", "scheduler")
DEFAULT_PENDING_WARN = 200
DEFAULT_LAG_WARN = 500


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_iso_ts(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _build_monitored_groups() -> list[dict[str, str]]:
    groups = [{"topic": TOPIC_TASK_DISPATCH, "group": "dispatcher"}]
    for topic in WATCHED_TOPICS:
        groups.append({"topic": topic, "group": "orchestrator"})
    seen: set[tuple[str, str]] = set()
    uniq: list[dict[str, str]] = []
    for item in groups:
        key = (item["topic"], item["group"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


def _evaluate_worker_heartbeats(
    rows: list[dict[str, Any]],
    *,
    stale_after_sec: int,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    workers_seen: set[str] = set()
    alive = 0
    stale = 0

    for row in rows:
        worker = str(row.get("worker") or "").strip()
        instance_id = str(row.get("instance_id") or "")
        status = str(row.get("status") or "unknown")
        ts = _parse_iso_ts(row.get("ts"))
        age_sec = _to_int((now - ts).total_seconds(), default=10**9) if ts else 10**9
        healthy = bool(ts and age_sec <= stale_after_sec and status not in {"stopping", "stopped"})
        if worker:
            workers_seen.add(worker)
        if healthy:
            alive += 1
        else:
            stale += 1
        items.append(
            {
                "worker": worker,
                "instance_id": instance_id,
                "status": status,
                "ts": ts.isoformat() if ts else "",
                "ageSec": age_sec if age_sec < 10**9 else None,
                "healthy": healthy,
                "extra": row.get("extra") if isinstance(row.get("extra"), dict) else {},
            }
        )

    missing = [name for name in REQUIRED_WORKERS if name not in workers_seen]
    for name in missing:
        items.append(
            {
                "worker": name,
                "instance_id": "",
                "status": "missing",
                "ts": "",
                "ageSec": None,
                "healthy": False,
                "extra": {},
            }
        )

    ok = not missing and stale == 0
    return {
        "ok": ok,
        "required": list(REQUIRED_WORKERS),
        "alive": alive,
        "stale": stale,
        "missing": missing,
        "items": items,
    }


async def collect_runtime_monitoring(
    bus: Any,
    *,
    stale_after_sec: int,
    pending_warn: int,
    lag_warn: int,
) -> dict[str, Any]:
    worker_rows = await bus.list_worker_heartbeats()
    worker_checks = _evaluate_worker_heartbeats(worker_rows, stale_after_sec=stale_after_sec)

    groups = _build_monitored_groups()
    groups_by_topic: dict[str, list[dict[str, Any]]] = {}
    for item in groups:
        topic = item["topic"]
        if topic not in groups_by_topic:
            groups_by_topic[topic] = await bus.stream_groups(topic)

    stream_items: list[dict[str, Any]] = []
    missing_groups = 0
    unhealthy_groups = 0
    pending_total = 0
    lag_total = 0

    for cfg in groups:
        topic = cfg["topic"]
        group = cfg["group"]
        group_info = None
        for row in groups_by_topic.get(topic, []):
            name = row.get("name")
            if str(name) == group:
                group_info = row
                break

        if group_info is None:
            missing_groups += 1
            unhealthy_groups += 1
            stream_items.append(
                {
                    "topic": topic,
                    "group": group,
                    "exists": False,
                    "healthy": False,
                    "reason": "missing-group",
                    "pending": 0,
                    "lag": 0,
                    "consumers": 0,
                    "maxConsumerPending": 0,
                }
            )
            continue

        pending_summary = await bus.pending_summary(topic, group)
        pending = _to_int(group_info.get("pending"), _to_int(pending_summary.get("count"), 0))
        lag = _to_int(group_info.get("lag"), 0)
        consumers = _to_int(group_info.get("consumers"), 0)
        max_consumer_pending = max((_to_int(c.get("pending"), 0) for c in pending_summary.get("consumers", [])), default=0)

        pending_total += pending
        lag_total += max(0, lag)
        healthy = pending <= pending_warn and lag <= lag_warn
        if not healthy:
            unhealthy_groups += 1

        stream_items.append(
            {
                "topic": topic,
                "group": group,
                "exists": True,
                "healthy": healthy,
                "pending": pending,
                "lag": lag,
                "consumers": consumers,
                "lastDeliveredId": str(group_info.get("last-delivered-id") or ""),
                "maxConsumerPending": max_consumer_pending,
                "pendingSummary": pending_summary,
            }
        )

    stream_checks = {
        "ok": unhealthy_groups == 0,
        "groups": stream_items,
        "totals": {
            "pending": pending_total,
            "lag": lag_total,
            "missingGroups": missing_groups,
            "unhealthyGroups": unhealthy_groups,
        },
    }
    return {"workers": worker_checks, "streams": stream_checks}


@router.get("/health/deep")
async def deep_health(db: AsyncSession = Depends(get_db)):
    """深度健康检查：Postgres、Redis、worker 心跳、consumer lag/pending。"""
    settings = get_settings()
    stale_after_sec = max(60, _to_int(getattr(settings, "heartbeat_interval_sec", 30), 30) * 3)
    pending_warn = DEFAULT_PENDING_WARN
    lag_warn = DEFAULT_LAG_WARN
    checks: dict[str, Any] = {
        "postgres": {"ok": False},
        "redis": {"ok": False},
        "workers": {"ok": False, "required": list(REQUIRED_WORKERS), "items": []},
        "streams": {"ok": False, "groups": []},
    }
    monitoring_error = ""

    # Postgres
    try:
        result = await db.execute(text("SELECT 1"))
        checks["postgres"]["ok"] = result.scalar() == 1
    except Exception as e:
        checks["postgres"]["error"] = str(e)

    # Redis
    try:
        bus = await get_event_bus()
        pong = await bus.redis.ping()
        checks["redis"]["ok"] = pong is True
        if checks["redis"]["ok"]:
            runtime_checks = await collect_runtime_monitoring(
                bus,
                stale_after_sec=stale_after_sec,
                pending_warn=pending_warn,
                lag_warn=lag_warn,
            )
            checks["workers"] = runtime_checks["workers"]
            checks["streams"] = runtime_checks["streams"]
    except Exception as e:
        checks["redis"]["error"] = str(e)
        monitoring_error = str(e)

    status = "ok"
    if not checks["postgres"]["ok"] or not checks["redis"]["ok"]:
        status = "degraded"
    elif not checks["workers"].get("ok", False) or not checks["streams"].get("ok", False):
        status = "degraded"

    payload = {
        "status": status,
        "checks": checks,
        "thresholds": {
            "workerStaleSec": stale_after_sec,
            "pendingWarn": pending_warn,
            "lagWarn": lag_warn,
        },
    }
    if monitoring_error:
        payload["monitoringError"] = monitoring_error
    return payload


@router.get("/pending-events")
async def pending_events(
    topic: str = "task.dispatch",
    group: str = "dispatcher",
    count: int = 20,
):
    """查看未 ACK 的 pending 事件（诊断工具）。"""
    bus = await get_event_bus()
    pending = await bus.get_pending(topic, group, count)
    summary = await bus.pending_summary(topic, group)
    return {
        "topic": topic,
        "group": group,
        "summary": summary,
        "pending": [
            {
                "entry_id": str(p.get("message_id", "")),
                "consumer": str(p.get("consumer", "")),
                "idle_ms": p.get("time_since_delivered", 0),
                "delivery_count": p.get("times_delivered", 0),
            }
            for p in pending
        ] if pending else [],
    }


@router.post("/migrate/check")
async def migration_check():
    """检查旧数据文件是否存在。"""
    data_dir = Path(__file__).parents[4] / "data"
    files = {
        "tasks_source": (data_dir / "tasks_source.json").exists(),
        "live_status": (data_dir / "live_status.json").exists(),
        "agent_config": (data_dir / "agent_config.json").exists(),
        "officials_stats": (data_dir / "officials_stats.json").exists(),
    }
    return {"data_dir": str(data_dir), "files": files}


@router.get("/config")
async def get_config():
    """获取当前运行配置（脱敏）。"""
    from ..config import get_settings
    settings = get_settings()
    return {
        "port": settings.port,
        "debug": settings.debug,
        "database": settings.database_url.split("@")[-1] if "@" in settings.database_url else "***",
        "redis": settings.redis_url.split("@")[-1] if "@" in settings.redis_url else settings.redis_url,
        "scheduler_scan_interval": settings.scheduler_scan_interval_seconds,
    }
