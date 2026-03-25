"""Read/write models for morning-brief compatibility endpoints."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

from .agent_config_service import data_dir, project_root, read_json
from .audit_service import record_task_audit
from .local_store import atomic_json_write, validate_url
from ..task_contract import ActorContext

log = logging.getLogger("edict.morning")

DEFAULT_MORNING_CONFIG = {
    "categories": [
        {"name": "政治", "enabled": True},
        {"name": "军事", "enabled": True},
        {"name": "经济", "enabled": True},
        {"name": "AI大模型", "enabled": True},
    ],
    "keywords": [],
    "custom_feeds": [],
    "feishu_webhook": "",
}


def _normalize_date(value: str | None) -> str:
    normalized = str(value or "").replace("-", "").strip()
    return normalized if normalized.isdigit() and len(normalized) == 8 else ""


def normalize_morning_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(raw or {})
    categories: list[dict[str, Any]] = []
    for item in data.get("categories") or DEFAULT_MORNING_CONFIG["categories"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        categories.append({"name": name, "enabled": bool(item.get("enabled", True))})
    if not categories:
        categories = list(DEFAULT_MORNING_CONFIG["categories"])

    keywords = [str(item).strip() for item in data.get("keywords") or [] if str(item).strip()]
    custom_feeds: list[dict[str, str]] = []
    for item in data.get("custom_feeds") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        category = str(item.get("category") or "").strip()
        if not name or not url or not category:
            continue
        custom_feeds.append({"name": name, "url": url, "category": category})

    return {
        "categories": categories,
        "keywords": keywords,
        "custom_feeds": custom_feeds,
        "feishu_webhook": str(data.get("feishu_webhook") or "").strip(),
    }


def normalize_morning_brief(raw: dict[str, Any] | None, *, date: str | None = None) -> dict[str, Any]:
    data = dict(raw or {})
    resolved_date = _normalize_date(date) or _normalize_date(data.get("date")) or str(data.get("date") or "").strip()
    categories_raw = data.get("categories") or {}
    categories: dict[str, list[dict[str, Any]]] = {}
    if isinstance(categories_raw, dict):
        for name, items in categories_raw.items():
            category_name = str(name or "").strip()
            if not category_name:
                continue
            normalized_items: list[dict[str, Any]] = []
            for item in items or []:
                if isinstance(item, dict):
                    normalized_items.append(dict(item))
            categories[category_name] = normalized_items
    return {
        "date": resolved_date,
        "generated_at": str(data.get("generated_at") or ""),
        "categories": categories,
    }


def project_morning_config_entries(audit_entries: list[dict[str, Any]]) -> dict[str, Any]:
    for item in reversed(audit_entries):
        payload = dict(item.get("payload") or {})
        config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        if isinstance(config, dict) and config.get("categories") is not None:
            return normalize_morning_config(config)
    return normalize_morning_config(DEFAULT_MORNING_CONFIG)


def project_morning_brief_entries(audit_entries: list[dict[str, Any]], *, date: str | None = None) -> dict[str, Any]:
    normalized_date = _normalize_date(date)
    for item in reversed(audit_entries):
        payload = dict(item.get("payload") or {})
        brief = payload.get("brief") if isinstance(payload.get("brief"), dict) else payload
        if not isinstance(brief, dict):
            continue
        candidate = normalize_morning_brief(brief)
        if normalized_date and candidate.get("date") != normalized_date:
            continue
        return candidate
    return normalize_morning_brief({}, date=normalized_date)


async def _load_snapshot_entries(action: str, limit: int = 30) -> list[dict[str, Any]]:
    try:
        from sqlalchemy import select

        from ..db import async_session
        from ..models.task_audit import TaskAudit
    except ModuleNotFoundError:
        log.warning("morning snapshot store unavailable; sqlalchemy dependency missing action=%s", action)
        return []

    async with async_session() as db:
        rows = (
            await db.execute(
                select(TaskAudit)
                .where(TaskAudit.action == action)
                .order_by(TaskAudit.ts.desc())
                .limit(limit)
            )
        ).scalars().all()
    return [row.to_dict() for row in reversed(rows)]


async def get_morning_brief(*, project_root_override: Path | None = None, date: str | None = None) -> dict[str, Any]:
    snapshots = await _load_snapshot_entries("morning.brief.snapshot", limit=60)
    if snapshots:
        brief = project_morning_brief_entries(snapshots, date=date)
        if brief.get("categories") or brief.get("date"):
            return brief

    base = data_dir(project_root_override=project_root_override)
    normalized_date = _normalize_date(date)
    if normalized_date:
        return normalize_morning_brief(read_json(base / f"morning_brief_{normalized_date}.json", {"date": normalized_date, "categories": {}}), date=normalized_date)
    return normalize_morning_brief(read_json(base / "morning_brief.json", {"categories": {}}))


async def get_morning_config(*, project_root_override: Path | None = None) -> dict[str, Any]:
    snapshots = await _load_snapshot_entries("morning.config.snapshot", limit=20)
    if snapshots:
        return project_morning_config_entries(snapshots)
    return normalize_morning_config(read_json(data_dir(project_root_override=project_root_override) / "morning_brief_config.json", DEFAULT_MORNING_CONFIG))


class MorningService:
    def __init__(
        self,
        *,
        project_root_override: Path | None = None,
        runner=None,
        thread_factory=None,
    ):
        self.project_root_override = project_root_override
        self.runner = runner or subprocess.run
        self.thread_factory = thread_factory or threading.Thread

    @property
    def _project_root(self) -> Path:
        return self.project_root_override or project_root()

    @property
    def _data_dir(self) -> Path:
        return data_dir(project_root_override=self.project_root_override)

    async def save_config(self, raw_config: dict[str, Any], actor: ActorContext) -> dict[str, Any]:
        config = normalize_morning_config(raw_config)
        webhook = config.get("feishu_webhook", "").strip()
        if webhook and not validate_url(webhook, allowed_schemes=("https",), allowed_domains=("open.feishu.cn", "open.larksuite.com")):
            deny_reason = "飞书 Webhook URL 无效，仅支持 https://open.feishu.cn 或 open.larksuite.com 域名"
            await record_task_audit(task_id="", action="morning.config.snapshot", actor=actor, allowed=False, deny_reason=deny_reason, payload={"config": config})
            return {"ok": False, "error": deny_reason}

        # Keep the legacy script input in sync during the migration window.
        atomic_json_write(self._data_dir / "morning_brief_config.json", config)
        await record_task_audit(task_id="", action="morning.config.snapshot", actor=actor, allowed=True, payload={"config": config})
        return {"ok": True, "message": "订阅配置已保存"}

    async def refresh_brief(self, actor: ActorContext, *, force: bool = True) -> dict[str, Any]:
        script = self._project_root / "scripts" / "fetch_morning_news.py"
        if not script.exists():
            deny_reason = f"采集脚本不存在: {script}"
            await record_task_audit(task_id="", action="morning.brief.refresh", actor=actor, allowed=False, deny_reason=deny_reason)
            return {"ok": False, "error": deny_reason}

        def _do_refresh() -> None:
            cmd = ["python3", str(script)]
            if force:
                cmd.append("--force")
            try:
                result = self.runner(cmd, capture_output=True, text=True, timeout=150)
                brief = normalize_morning_brief(read_json(self._data_dir / "morning_brief.json", {"categories": {}}))
                asyncio.run(
                    record_task_audit(
                        task_id="",
                        action="morning.brief.snapshot",
                        actor=actor,
                        allowed=getattr(result, "returncode", 1) == 0,
                        deny_reason="" if getattr(result, "returncode", 1) == 0 else (getattr(result, "stderr", "") or getattr(result, "stdout", ""))[:200],
                        payload={
                            "brief": brief,
                            "force": force,
                            "exitCode": getattr(result, "returncode", 1),
                        },
                    )
                )
            except Exception as exc:
                asyncio.run(
                    record_task_audit(
                        task_id="",
                        action="morning.brief.refresh",
                        actor=actor,
                        allowed=False,
                        deny_reason=str(exc),
                        payload={"force": force},
                    )
                )

        self.thread_factory(target=_do_refresh, daemon=True).start()
        return {"ok": True, "message": "采集已触发，约30-60秒后刷新"}
