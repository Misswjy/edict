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

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.task_contract import TaskState, canonicalize_lane, canonicalize_state, ensure_task_shape
from app.services.court_discuss_service import (
    COURT_DISCUSS_SNAPSHOT_ACTION,
    normalize_session,
    serialize_session,
)

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


async def migrate(file_path: Path, dry_run: bool = False):
    """执行迁移。"""
    from app.db import async_session
    from app.models.task import Task
    from app.models.task_audit import TaskAudit

    if not file_path.exists():
        log.error(f"数据文件不存在: {file_path}")
        return

    # 读取旧版数据
    raw = file_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    old_tasks = payload.get("tasks", payload) if isinstance(payload, dict) else payload
    if not isinstance(old_tasks, list):
        raise ValueError("tasks_source.json 必须是任务数组或包含 tasks 数组的对象")
    log.info(f"读取到 {len(old_tasks)} 个旧版任务")

    # 统计
    stats = {"total": len(old_tasks), "migrated": 0, "skipped": 0, "errors": 0}
    by_state = {}

    for old in old_tasks:
        state_str = old.get("state", "?")
        by_state[state_str] = by_state.get(state_str, 0) + 1

    log.info(f"状态分布: {by_state}")

    if dry_run:
        log.info("=== DRY RUN 模式，不写入数据库 ===")
        for old in old_tasks:
            params = parse_old_task(old)
            log.info(f"  [{params['id']}] {params['title'][:40]} → {params['state'].value}")
        model_log_path = file_path.parent / "model_change_log.json"
        if model_log_path.exists():
            try:
                model_entries = json.loads(model_log_path.read_text(encoding="utf-8"))
            except Exception:
                model_entries = []
            log.info(f"Dry run 检测到 {len(model_entries) if isinstance(model_entries, list) else 0} 条模型变更历史待导入")
        log.info(f"Dry run 完成: {stats['total']} 个任务待迁移")
        return

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

        model_log_path = file_path.parent / "model_change_log.json"
        if model_log_path.exists():
            try:
                model_entries = json.loads(model_log_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.error(f"❌ 读取模型变更历史失败: {exc}")
                model_entries = []
            if isinstance(model_entries, list):
                imported_model_changes = 0
                skipped_model_changes = 0
                for entry in model_entries:
                    params = parse_legacy_model_change(entry)
                    if not params:
                        skipped_model_changes += 1
                        continue
                    existing = await db.get(TaskAudit, params["audit_id"])
                    if existing:
                        skipped_model_changes += 1
                        continue
                    db.add(TaskAudit(**params))
                    imported_model_changes += 1
                log.info("模型变更历史导入: 成功 %s, 跳过 %s", imported_model_changes, skipped_model_changes)

        court_sessions_path = file_path.parent / "court_discuss_sessions.json"
        if court_sessions_path.exists():
            try:
                court_sessions = json.loads(court_sessions_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.error(f"❌ 读取朝堂议政历史失败: {exc}")
                court_sessions = {}
            imported_court_sessions = 0
            skipped_court_sessions = 0
            if isinstance(court_sessions, dict):
                for item in court_sessions.values():
                    params = parse_legacy_court_session(item)
                    if not params:
                        skipped_court_sessions += 1
                        continue
                    existing = await db.get(TaskAudit, params["audit_id"])
                    if existing:
                        skipped_court_sessions += 1
                        continue
                    db.add(TaskAudit(**params))
                    imported_court_sessions += 1
            log.info("朝堂议政历史导入: 成功 %s, 跳过 %s", imported_court_sessions, skipped_court_sessions)

        await db.commit()

    log.info(f"迁移完成: 总计 {stats['total']}, 成功 {stats['migrated']}, "
             f"跳过 {stats['skipped']}, 错误 {stats['errors']}")


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
