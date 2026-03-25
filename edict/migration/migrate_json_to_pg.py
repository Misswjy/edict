#!/usr/bin/env python3
"""JSON → Postgres 数据迁移脚本。

读取旧版 data/tasks_source.json，导入到 Edict Postgres 数据库。

用法:
  # 确保 Postgres 已运行且 schema 已创建（alembic upgrade head）
  python3 migrate_json_to_pg.py

  # 指定数据文件
  python3 migrate_json_to_pg.py --file /path/to/tasks_source.json

  # Dry run（只分析不写入）
  python3 migrate_json_to_pg.py --dry-run
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.task_contract import TaskState, canonicalize_lane, canonicalize_state, ensure_task_shape
from app.services.court_discuss_service import (
    COURT_DISCUSS_SNAPSHOT_ACTION,
    normalize_session,
    serialize_session,
)
from app.services.morning_service import normalize_morning_brief, normalize_morning_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("migrate")

LEGACY_STATE_FALLBACKS = {
    "Inbox": TaskState.Sili.value,
    "Todo": TaskState.Pending.value,
    "": TaskState.Pending.value,
}


def parse_legacy_datetime(value: str | None, fallback: datetime | None = None) -> datetime:
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return fallback or datetime.now(timezone.utc)


def coerce_task_state(value: str | None) -> TaskState:
    raw = str(value or "").strip()
    candidate = LEGACY_STATE_FALLBACKS.get(raw, raw)
    candidate = canonicalize_state(candidate) or TaskState.Pending.value
    try:
        return TaskState(candidate)
    except ValueError:
        return TaskState.Pending


def parse_old_task(old: dict) -> dict:
    """将旧版 task JSON 转换为当前 ORM 对齐的 Task 参数。"""
    raw = dict(old or {})
    original_state = raw.get("state")
    if "consult_log" in raw and "consultLog" not in raw:
        raw["consultLog"] = raw["consult_log"]
    if "scheduler" in raw and "_scheduler" not in raw:
        raw["_scheduler"] = raw["scheduler"]
    normalized = ensure_task_shape(raw)
    task_id = str(normalized.get("id") or f"LEGACY-{uuid.uuid4().hex[:12].upper()}")
    created_at = parse_legacy_datetime(normalized.get("createdAt"), fallback=parse_legacy_datetime(normalized.get("updatedAt")))
    updated_at = parse_legacy_datetime(normalized.get("updatedAt"), fallback=created_at)
    archived = bool(normalized.get("archived"))
    archived_at = parse_legacy_datetime(old.get("archivedAt"), fallback=updated_at) if archived and old.get("archivedAt") else None
    state = coerce_task_state(original_state or normalized.get("state"))
    return {
        "id": task_id,
        "title": str(normalized.get("title") or "未命名任务"),
        "official": str(normalized.get("official") or ""),
        "org": str(normalized.get("org") or ""),
        "state": state,
        "now": str(normalized.get("now") or ""),
        "eta": str(normalized.get("eta") or "-"),
        "block": str(normalized.get("block") or "无"),
        "output": str(normalized.get("output") or ""),
        "ac": str(normalized.get("ac") or ""),
        "priority": str(normalized.get("priority") or "normal"),
        "lane": canonicalize_lane(normalized.get("lane")),
        "review_round": int(normalized.get("review_round") or 0),
        "state_version": max(1, int(normalized.get("_stateVersion") or 1)),
        "archived": archived,
        "archived_at": archived_at,
        "flow_log": list(normalized.get("flow_log") or []),
        "progress_log": list(normalized.get("progress_log") or []),
        "consult_log": list(normalized.get("consultLog") or []),
        "todos": list(normalized.get("todos") or []),
        "scheduler": dict(normalized.get("_scheduler") or {}),
        "template_id": str(normalized.get("templateId") or ""),
        "template_params": dict(normalized.get("templateParams") or {}),
        "target_dept": str(normalized.get("targetDept") or ""),
        "prev_state": str(normalized.get("_prev_state") or ""),
        "created_at": created_at,
        "updated_at": updated_at,
    }


def parse_legacy_model_change(entry: dict) -> dict[str, object] | None:
    raw = dict(entry or {})
    agent_id = str(raw.get("agentId") or "").strip()
    old_model = str(raw.get("oldModel") or "").strip()
    new_model = str(raw.get("newModel") or "").strip()
    if not agent_id or (not old_model and not new_model):
        return None

    changed_at = parse_legacy_datetime(raw.get("at"))
    rolled_back = bool(raw.get("rolledBack"))
    stable_key = f"legacy-model-change:{changed_at.isoformat()}:{agent_id}:{old_model}:{new_model}:{int(rolled_back)}"
    request_id = f"legacy-model:{agent_id}:{changed_at.strftime('%Y%m%d%H%M%S')}"[:64]
    return {
        "audit_id": uuid.uuid5(uuid.NAMESPACE_URL, stable_key),
        "ts": changed_at,
        "request_id": request_id,
        "task_id": "",
        "action": "config.set_model",
        "actor_id": "migration",
        "actor_type": "system",
        "source": "legacy-import",
        "signature_verified": False,
        "from_state": "",
        "to_state": "",
        "target_agent": agent_id,
        "allowed": not rolled_back,
        "deny_reason": "gateway restart failed" if rolled_back else "",
        "policy_version": "legacy-import",
        "payload": {
            "oldModel": old_model,
            "newModel": new_model,
            "rolledBack": rolled_back,
            "importedFrom": "data/model_change_log.json",
        },
        "payload_hash": "",
        "payload_summary": f"{agent_id}: {old_model} -> {new_model}",
    }


def _file_timestamp(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def parse_legacy_dispatch_channel(entry: dict, *, source_path: Path) -> dict[str, object] | None:
    raw = dict(entry or {})
    channel = str(raw.get("dispatchChannel") or "").strip()
    if not channel:
        return None

    recorded_at = _file_timestamp(source_path)
    stable_key = f"legacy-dispatch-channel:{channel}:{source_path.name}"
    return {
        "audit_id": uuid.uuid5(uuid.NAMESPACE_URL, stable_key),
        "ts": recorded_at,
        "request_id": "legacy-dispatch-channel"[:64],
        "task_id": "",
        "action": "config.set_dispatch_channel",
        "actor_id": "migration",
        "actor_type": "system",
        "source": "legacy-import",
        "signature_verified": False,
        "from_state": "",
        "to_state": "",
        "target_agent": "",
        "allowed": True,
        "deny_reason": "",
        "policy_version": "legacy-import",
        "payload": {
            "channel": channel,
            "importedFrom": f"data/{source_path.name}",
        },
        "payload_hash": "",
        "payload_summary": f"dispatch channel -> {channel}",
    }


def parse_legacy_morning_config(entry: dict, *, source_path: Path) -> dict[str, object] | None:
    if not source_path.exists():
        return None
    config = normalize_morning_config(entry)
    if not config.get("categories"):
        return None

    recorded_at = _file_timestamp(source_path)
    stable_key = f"legacy-morning-config:{json.dumps(config, ensure_ascii=False, sort_keys=True)}"
    return {
        "audit_id": uuid.uuid5(uuid.NAMESPACE_URL, stable_key),
        "ts": recorded_at,
        "request_id": "legacy-morning-config"[:64],
        "task_id": "",
        "action": "morning.config.snapshot",
        "actor_id": "migration",
        "actor_type": "system",
        "source": "legacy-import",
        "signature_verified": False,
        "from_state": "",
        "to_state": "",
        "target_agent": "",
        "allowed": True,
        "deny_reason": "",
        "policy_version": "legacy-import",
        "payload": {
            "config": config,
            "importedFrom": f"data/{source_path.name}",
        },
        "payload_hash": "",
        "payload_summary": "morning brief config snapshot",
    }


def parse_legacy_morning_brief(entry: dict, *, source_path: Path) -> dict[str, object] | None:
    brief = normalize_morning_brief(entry)
    if not brief.get("categories") and not brief.get("date"):
        return None

    recorded_at = parse_legacy_datetime(
        brief.get("generated_at"),
        fallback=parse_legacy_datetime(brief.get("date"), fallback=_file_timestamp(source_path)),
    )
    stable_key = (
        f"legacy-morning-brief:{source_path.name}:"
        f"{brief.get('date')}:{brief.get('generated_at')}:"
        f"{json.dumps(brief.get('categories') or {}, ensure_ascii=False, sort_keys=True)}"
    )
    return {
        "audit_id": uuid.uuid5(uuid.NAMESPACE_URL, stable_key),
        "ts": recorded_at,
        "request_id": f"legacy-morning:{brief.get('date') or source_path.stem}"[:64],
        "task_id": "",
        "action": "morning.brief.snapshot",
        "actor_id": "migration",
        "actor_type": "system",
        "source": "legacy-import",
        "signature_verified": False,
        "from_state": "",
        "to_state": "",
        "target_agent": "",
        "allowed": True,
        "deny_reason": "",
        "policy_version": "legacy-import",
        "payload": {
            "brief": brief,
            "importedFrom": f"data/{source_path.name}",
        },
        "payload_hash": "",
        "payload_summary": f"morning brief {brief.get('date') or source_path.stem}",
    }


def parse_legacy_court_session(entry: dict) -> dict[str, object] | None:
    normalized = normalize_session(entry)
    if not normalized:
        return None

    session_id = str(normalized["session_id"])
    updated_at = parse_legacy_datetime(str(normalized.get("updated_at") or ""))
    stable_key = f"legacy-court-session:{session_id}:{updated_at.isoformat()}:{normalized.get('phase')}:{normalized.get('round')}"
    return {
        "audit_id": uuid.uuid5(uuid.NAMESPACE_URL, stable_key),
        "ts": updated_at,
        "request_id": f"legacy-court:{session_id}"[:64],
        "task_id": str(normalized.get("task_id") or ""),
        "action": COURT_DISCUSS_SNAPSHOT_ACTION,
        "actor_id": "migration",
        "actor_type": "system",
        "source": "legacy-import",
        "signature_verified": False,
        "from_state": "",
        "to_state": "",
        "target_agent": "",
        "allowed": True,
        "deny_reason": "",
        "policy_version": "legacy-import",
        "payload": {
            "event": "legacy-import",
            "session": serialize_session(normalized),
        },
        "payload_hash": "",
        "payload_summary": f"court session {session_id}",
    }


def analyze_migration_sources(file_path: Path) -> dict[str, Any]:
    raw_payload = json.loads(file_path.read_text(encoding="utf-8"))
    old_tasks = raw_payload.get("tasks", raw_payload) if isinstance(raw_payload, dict) else raw_payload
    if not isinstance(old_tasks, list):
        raise ValueError("tasks_source.json 必须是任务数组或包含 tasks 数组的对象")

    by_state: dict[str, int] = {}
    normalized_state: dict[str, int] = {}
    parse_errors: list[str] = []
    archived_count = 0
    review_rounds = 0

    for old in old_tasks:
        state_str = str(old.get("state", "?"))
        by_state[state_str] = by_state.get(state_str, 0) + 1
        try:
            params = parse_old_task(old)
            normalized = params["state"].value
            normalized_state[normalized] = normalized_state.get(normalized, 0) + 1
            archived_count += int(bool(params["archived"]))
            if int(params["review_round"] or 0) > 0:
                review_rounds += 1
        except Exception as exc:
            parse_errors.append(f"{old.get('id', '?')}: {exc}")

    model_entries = _read_json(file_path.parent / "model_change_log.json", [])
    court_sessions = _read_json(file_path.parent / "court_discuss_sessions.json", {})
    morning_config = _read_json(file_path.parent / "morning_brief_config.json", {})
    morning_config_path = file_path.parent / "morning_brief_config.json"
    morning_brief_files = [
        path
        for path in sorted(file_path.parent.glob("morning_brief*.json"))
        if path.name != "morning_brief_config.json"
    ]
    agent_config = _read_json(file_path.parent / "agent_config.json", {})

    report = {
        "source": str(file_path),
        "tasks": {
            "total": len(old_tasks),
            "archived": archived_count,
            "withReviewRounds": review_rounds,
            "byState": by_state,
            "normalizedState": normalized_state,
            "parseErrors": parse_errors,
        },
        "sidecars": {
            "modelChanges": len(model_entries) if isinstance(model_entries, list) else 0,
            "courtDiscussSessions": len(court_sessions) if isinstance(court_sessions, dict) else 0,
            "morningConfigPresent": bool(parse_legacy_morning_config(morning_config, source_path=morning_config_path)) if isinstance(morning_config, dict) else False,
            "morningBriefFiles": [path.name for path in morning_brief_files],
            "dispatchChannel": str(agent_config.get("dispatchChannel") or "").strip() if isinstance(agent_config, dict) else "",
        },
    }
    return report


async def migrate(file_path: Path, dry_run: bool = False):
    """执行迁移。"""
    if not file_path.exists():
        log.error(f"数据文件不存在: {file_path}")
        return

    report = analyze_migration_sources(file_path)

    raw = file_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    old_tasks = payload.get("tasks", payload) if isinstance(payload, dict) else payload
    if not isinstance(old_tasks, list):
        raise ValueError("tasks_source.json 必须是任务数组或包含 tasks 数组的对象")
    log.info("读取到 %s 个旧版任务", len(old_tasks))
    log.info("状态分布: %s", report["tasks"]["byState"])

    stats = {
        "total": len(old_tasks),
        "migrated": 0,
        "skipped": 0,
        "errors": 0,
        "audits": {
            "modelChanges": {"imported": 0, "skipped": 0},
            "courtDiscussSessions": {"imported": 0, "skipped": 0},
            "morningConfig": {"imported": 0, "skipped": 0},
            "morningBriefs": {"imported": 0, "skipped": 0},
            "dispatchChannel": {"imported": 0, "skipped": 0},
        },
    }

    if dry_run:
        log.info("=== DRY RUN 模式，不写入数据库 ===")
        for old in old_tasks:
            params = parse_old_task(old)
            log.info(f"  [{params['id']}] {params['title'][:40]} → {params['state'].value}")
        log.info("Dry run 附属数据: %s", json.dumps(report["sidecars"], ensure_ascii=False))
        if report["tasks"]["parseErrors"]:
            log.warning("Dry run 发现 %s 条任务解析错误", len(report["tasks"]["parseErrors"]))
        log.info("Dry run 完成: %s", json.dumps(report, ensure_ascii=False))
        return report

    from app.db import async_session
    from app.models.task import Task
    from app.models.task_audit import TaskAudit

    # 写入 Postgres
    async with async_session() as db:
        for old in old_tasks:
            try:
                params = parse_old_task(old)
                existing = await db.get(Task, params["id"])
                if existing:
                    log.debug(f"跳过已存在: {params['id']}")
                    stats["skipped"] += 1
                    continue

                task = Task(**params)
                db.add(task)
                stats["migrated"] += 1
                log.info(f"✅ 迁移: [{params['id']}] {params['title'][:40]} → {params['state'].value}")

            except Exception as e:
                log.error(f"❌ 迁移失败: {old.get('id', '?')}: {e}")
                stats["errors"] += 1

        model_entries = _read_json(file_path.parent / "model_change_log.json", [])
        if isinstance(model_entries, list):
            for entry in model_entries:
                params = parse_legacy_model_change(entry)
                if not params:
                    stats["audits"]["modelChanges"]["skipped"] += 1
                    continue
                existing = await db.get(TaskAudit, params["audit_id"])
                if existing:
                    stats["audits"]["modelChanges"]["skipped"] += 1
                    continue
                db.add(TaskAudit(**params))
                stats["audits"]["modelChanges"]["imported"] += 1
            log.info(
                "模型变更历史导入: 成功 %s, 跳过 %s",
                stats["audits"]["modelChanges"]["imported"],
                stats["audits"]["modelChanges"]["skipped"],
            )

        court_sessions = _read_json(file_path.parent / "court_discuss_sessions.json", {})
        if isinstance(court_sessions, dict):
            for item in court_sessions.values():
                params = parse_legacy_court_session(item)
                if not params:
                    stats["audits"]["courtDiscussSessions"]["skipped"] += 1
                    continue
                existing = await db.get(TaskAudit, params["audit_id"])
                if existing:
                    stats["audits"]["courtDiscussSessions"]["skipped"] += 1
                    continue
                db.add(TaskAudit(**params))
                stats["audits"]["courtDiscussSessions"]["imported"] += 1
            log.info(
                "朝堂议政历史导入: 成功 %s, 跳过 %s",
                stats["audits"]["courtDiscussSessions"]["imported"],
                stats["audits"]["courtDiscussSessions"]["skipped"],
            )

        morning_config_path = file_path.parent / "morning_brief_config.json"
        morning_config = _read_json(morning_config_path, {})
        if isinstance(morning_config, dict):
            params = parse_legacy_morning_config(morning_config, source_path=morning_config_path)
            if params:
                existing = await db.get(TaskAudit, params["audit_id"])
                if existing:
                    stats["audits"]["morningConfig"]["skipped"] += 1
                else:
                    db.add(TaskAudit(**params))
                    stats["audits"]["morningConfig"]["imported"] += 1
            elif morning_config_path.exists():
                stats["audits"]["morningConfig"]["skipped"] += 1

        morning_brief_paths = [
            path
            for path in sorted(file_path.parent.glob("morning_brief*.json"))
            if path.name != "morning_brief_config.json"
        ]
        for morning_path in morning_brief_paths:
            params = parse_legacy_morning_brief(_read_json(morning_path, {}), source_path=morning_path)
            if not params:
                stats["audits"]["morningBriefs"]["skipped"] += 1
                continue
            existing = await db.get(TaskAudit, params["audit_id"])
            if existing:
                stats["audits"]["morningBriefs"]["skipped"] += 1
                continue
            db.add(TaskAudit(**params))
            stats["audits"]["morningBriefs"]["imported"] += 1
        if morning_brief_paths:
            log.info(
                "朝报快照导入: 成功 %s, 跳过 %s",
                stats["audits"]["morningBriefs"]["imported"],
                stats["audits"]["morningBriefs"]["skipped"],
            )

        agent_config_path = file_path.parent / "agent_config.json"
        agent_config = _read_json(agent_config_path, {})
        if isinstance(agent_config, dict):
            params = parse_legacy_dispatch_channel(agent_config, source_path=agent_config_path)
            if params:
                existing = await db.get(TaskAudit, params["audit_id"])
                if existing:
                    stats["audits"]["dispatchChannel"]["skipped"] += 1
                else:
                    db.add(TaskAudit(**params))
                    stats["audits"]["dispatchChannel"]["imported"] += 1

        await db.commit()

    log.info(
        "迁移完成: 总计 %s, 成功 %s, 跳过 %s, 错误 %s, 审计=%s",
        stats["total"],
        stats["migrated"],
        stats["skipped"],
        stats["errors"],
        json.dumps(stats["audits"], ensure_ascii=False),
    )
    return {"report": report, "stats": stats}


def main():
    parser = argparse.ArgumentParser(description="Migrate JSON tasks to Postgres")
    parser.add_argument(
        "--file", "-f",
        default=str(Path(__file__).parent.parent.parent / "data" / "tasks_source.json"),
        help="Path to tasks_source.json",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only analyze, don't write")
    args = parser.parse_args()

    asyncio.run(migrate(Path(args.file), dry_run=args.dry_run))


if __name__ == "__main__":
    main()
