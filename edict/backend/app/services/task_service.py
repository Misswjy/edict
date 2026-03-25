"""Task service aligned with the shared task contract and backend control plane."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..event_contract import (
    TOPIC_AGENT_TODO_UPDATE,
    TOPIC_TASK_COMPLETED,
    TOPIC_TASK_CREATED,
    TOPIC_TASK_DISPATCH,
    TOPIC_TASK_ESCALATED,
    TOPIC_TASK_STALLED,
    TOPIC_TASK_STATUS,
    build_auto_dispatch_key,
    build_consult_dispatch_key,
    build_manual_dispatch_key,
    build_task_created_dedupe_key,
    build_task_escalated_dedupe_key,
    build_task_stalled_dedupe_key,
    build_task_state_dedupe_key,
    build_task_todos_dedupe_key,
)
from ..models.task import Task
from ..task_contract import (
    TERMINAL_STATES,
    ActorContext,
    TaskState,
    authorize_consultation,
    authorize_dispatch,
    authorize_progress,
    authorize_review,
    authorize_scheduler,
    authorize_task_action,
    authorize_todos,
    authorize_transition,
    build_audit_entry,
    canonicalize_lane,
    canonicalize_state,
    central_queue_owner,
    ensure_execution_assignment,
    ensure_task_shape,
    fast_lane_ready,
    is_fast_lane,
    make_actor_context,
    next_manual_transition,
    queue_sla_seconds,
    resolve_dispatch_agent,
    resolve_state_org,
    review_transition,
    validate_transition,
)
from .event_bus import EventBus

log = logging.getLogger("edict.task_service")


class TaskService:
    def __init__(self, db: AsyncSession, event_bus: EventBus):
        self.db = db
        self.bus = event_bus

    async def create_task(
        self,
        title: str,
        official: str = "中书令",
        priority: str = "normal",
        lane: str = "standard",
        template_id: str = "",
        template_params: dict | None = None,
        target_dept: str = "",
        initial_state: TaskState = TaskState.Sili,
        actor: ActorContext | None = None,
    ) -> Task:
        actor = actor or make_actor_context("system", source="edict-backend")
        now = datetime.now(timezone.utc).isoformat()
        task: Task | None = None
        last_error: IntegrityError | None = None

        for attempt in range(1, 6):
            task_id = await self._next_task_id()
            scheduler = self._default_scheduler(updated_at=now)
            task = Task(
                id=task_id,
                title=title,
                official=official,
                org="司礼监",
                state=initial_state,
                now="等待司礼监接旨分办",
                priority=priority,
                lane=canonicalize_lane(lane),
                template_id=template_id,
                template_params=template_params or {},
                target_dept=target_dept or "",
                state_version=1,
                flow_log=[
                    {
                        "at": now,
                        "from": "皇上",
                        "to": "司礼监",
                        "remark": f"下旨：{title}",
                    }
                ],
                progress_log=[],
                consult_log=[],
                todos=[],
                scheduler=scheduler,
            )
            self.db.add(task)
            try:
                await self.db.flush()
                await self.db.commit()
                break
            except IntegrityError as exc:
                last_error = exc
                await self.db.rollback()
                log.warning("task id collision on create_task attempt=%s title=%s retrying", attempt, title)
        else:
            assert last_error is not None
            raise last_error

        assert task is not None

        await self.bus.publish(
            topic=TOPIC_TASK_CREATED,
            trace_id=task.id,
            event_type="task.created",
            producer=actor.actor_id,
            payload=task.to_dict(),
            meta=self._event_meta(actor, task),
            dedupe_key=build_task_created_dedupe_key(task.id, int(task.state_version or 1)),
        )
        await self._audit("task.create", actor, True, task_id=task.id, to_state=task.state.value, payload={"title": title})
        return task

    async def transition_state(
        self,
        task_id: str,
        new_state: TaskState,
        actor: ActorContext | None = None,
        reason: str = "",
    ) -> Task:
        actor = actor or make_actor_context("system", source="edict-backend")
        task = await self._get_task(task_id, for_update=True)
        task_dict = task.to_dict()
        current_state = canonicalize_state(task_dict.get("state"))

        allowed, deny_reason = authorize_transition(actor, task_dict, new_state.value)
        if not allowed:
            await self._audit(
                "task.transition",
                actor,
                False,
                task_id=task_id,
                from_state=current_state,
                to_state=new_state.value,
                deny_reason=deny_reason,
                payload={"reason": reason},
            )
            raise PermissionError(deny_reason)

        valid, validation_error = validate_transition(task_dict, new_state.value)
        if not valid:
            await self._audit(
                "task.transition",
                actor,
                False,
                task_id=task_id,
                from_state=current_state,
                to_state=new_state.value,
                deny_reason=validation_error,
                payload={"reason": reason},
            )
            raise ValueError(validation_error)

        old_org = task.org
        task_dict["state"] = new_state.value
        task_dict["now"] = reason or task.now
        self._bump_state_version(task, task_dict)
        new_org = resolve_state_org(task_dict, new_state.value) or task.org
        task_dict["org"] = new_org
        task_dict.setdefault("flow_log", []).append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "from": old_org,
                "to": new_org,
                "remark": reason or f"状态推进到 {new_state.value}",
            }
        )
        self._apply_task_dict(task, task_dict)
        task.state = new_state
        task.org = new_org
        task.updated_at = datetime.now(timezone.utc)
        await self.db.commit()

        await self._publish_state_event(task, actor, current_state, reason)
        await self._maybe_fast_track_assigned(task)
        await self._audit("task.transition", actor, True, task_id=task.id, from_state=current_state, to_state=new_state.value, payload={"reason": reason})
        return task

    async def request_dispatch(
        self,
        task_id: str,
        target_agent: str,
        actor: ActorContext | None = None,
        message: str = "",
    ) -> None:
        actor = actor or make_actor_context("system", source="edict-backend")
        task = await self._get_task(task_id)
        task_dict = task.to_dict()
        allowed, deny_reason = authorize_dispatch(actor, task_dict, target_agent)
        if not allowed:
            await self._audit(
                "task.dispatch",
                actor,
                False,
                task_id=task_id,
                from_state=task.state.value,
                to_state=task.state.value,
                target_agent=target_agent,
                deny_reason=deny_reason,
                payload={"message": message},
            )
            raise PermissionError(deny_reason)

        await self._publish_dispatch_request(
            task,
            actor,
            target_agent,
            message=message,
            manual=True,
        )
        await self._audit(
            "task.dispatch",
            actor,
            True,
            task_id=task_id,
            from_state=task.state.value,
            to_state=task.state.value,
            target_agent=target_agent,
            payload={"message": message},
        )

    async def request_consultation(
        self,
        task_id: str,
        target_agent: str,
        note: str = "",
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        actor = actor or make_actor_context("system", source="edict-backend")
        task = await self._get_task(task_id, for_update=True)
        task_dict = task.to_dict()
        allowed, deny_reason = authorize_consultation(actor, task_dict, target_agent)
        if not allowed:
            await self._audit(
                "task.consult",
                actor,
                False,
                task_id=task.id,
                from_state=task.state.value,
                to_state=task.state.value,
                target_agent=target_agent,
                deny_reason=deny_reason,
                payload={"note": note},
            )
            raise PermissionError(deny_reason)

        consult_key = build_consult_dispatch_key(
            task.id,
            task.state.value,
            int(task.state_version or 1),
            actor.actor_id,
            target_agent,
            actor.request_id,
        )
        consult_entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "from": actor.actor_id,
            "to": target_agent,
            "state": task.state.value,
            "note": note or "横向咨询",
            "consultKey": consult_key,
        }
        task_dict.setdefault("consultLog", []).append(consult_entry)
        self._apply_task_dict(task, task_dict)
        task.updated_at = datetime.now(timezone.utc)
        await self.db.commit()

        await self.bus.publish(
            topic=TOPIC_TASK_DISPATCH,
            trace_id=task.id,
            event_type="task.consult.request",
            producer=actor.actor_id,
            payload={
                "task_id": task.id,
                "agent": target_agent,
                "message": f"横向咨询，请保持任务主状态不变：{note or task.title}",
                "state": task.state.value,
                "dispatch_key": consult_key,
                "version": int(task.state_version or 1),
                "task": task.to_dict(),
                "consult": True,
            },
            meta=self._event_meta(actor, task, {"dispatch_key": consult_key, "consult": True}),
            dedupe_key=consult_key,
        )
        await self._audit(
            "task.consult",
            actor,
            True,
            task_id=task.id,
            from_state=task.state.value,
            to_state=task.state.value,
            target_agent=target_agent,
            payload={"note": note},
        )
        return {"ok": True, "message": f"{task.id} 已向 {target_agent} 发起横向咨询", "consultKey": consult_key}

    async def add_progress(self, task_id: str, actor: ActorContext | None, content: str) -> Task:
        actor = actor or make_actor_context("system", source="edict-backend")
        task = await self._get_task(task_id, for_update=True)
        task_dict = task.to_dict()
        allowed, deny_reason = authorize_progress(actor, task_dict)
        if not allowed:
            await self._audit("task.progress", actor, False, task_id=task_id, from_state=task.state.value, deny_reason=deny_reason, payload={"content": content})
            raise PermissionError(deny_reason)

        progress_log = list(task.progress_log or [])
        progress_log.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "agent": actor.actor_id,
                "text": content,
                "state": task.state.value,
                "org": task.org,
            }
        )
        task.progress_log = progress_log
        task.now = content
        task.updated_at = datetime.now(timezone.utc)
        scheduler = self._ensure_scheduler(task_dict)
        scheduler["lastProgressAt"] = datetime.now(timezone.utc).isoformat()
        scheduler["stallSince"] = None
        scheduler["retryCount"] = 0
        scheduler["escalationLevel"] = 0
        task.scheduler = scheduler
        await self.db.commit()
        await self._audit("task.progress", actor, True, task_id=task_id, from_state=task.state.value, payload={"content": content})
        return task

    async def update_todos(self, task_id: str, actor: ActorContext | None, todos: list[dict]) -> Task:
        actor = actor or make_actor_context("system", source="edict-backend")
        task = await self._get_task(task_id, for_update=True)
        task_dict = task.to_dict()
        allowed, deny_reason = authorize_todos(actor, task_dict)
        if not allowed:
            await self._audit("task.todos", actor, False, task_id=task_id, from_state=task.state.value, deny_reason=deny_reason, payload={"todo_count": len(todos)})
            raise PermissionError(deny_reason)
        task.todos = list(todos or [])
        task.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self.bus.publish(
            topic=TOPIC_AGENT_TODO_UPDATE,
            trace_id=task.id,
            event_type="task.todos.updated",
            producer=actor.actor_id,
            payload={"task_id": task.id, "items": task.todos, "source_of_truth": "tasks.todos"},
            meta=self._event_meta(actor, task, {"source_of_truth": "tasks.todos"}),
            dedupe_key=build_task_todos_dedupe_key(task.id, int(task.state_version or 1), actor.request_id),
        )
        await self._audit("task.todos", actor, True, task_id=task_id, from_state=task.state.value, payload={"todo_count": len(todos), "source_of_truth": "tasks.todos"})
        return task

    async def update_scheduler(self, task_id: str, actor: ActorContext | None, scheduler: dict) -> Task:
        actor = actor or make_actor_context("system", source="edict-backend")
        allowed, deny_reason = authorize_scheduler(actor, "update_scheduler")
        if not allowed:
            await self._audit("task.scheduler", actor, False, task_id=task_id, deny_reason=deny_reason, payload={"scheduler": scheduler})
            raise PermissionError(deny_reason)
        task = await self._get_task(task_id, for_update=True)
        task.scheduler = scheduler
        task.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self._audit("task.scheduler", actor, True, task_id=task_id, from_state=task.state.value, payload={"scheduler": scheduler})
        return task

    async def task_action(self, task_id: str, action: str, reason: str = "", actor: ActorContext | None = None) -> dict[str, Any]:
        actor = actor or make_actor_context("emperor", source="dashboard")
        task = await self._get_task(task_id, for_update=True)
        task_dict = task.to_dict()
        allowed, deny_reason = authorize_task_action(actor, task_dict, action)
        old_state = canonicalize_state(task_dict.get("state"))
        if not allowed:
            await self._audit(f"task.{action}", actor, False, task_id=task_id, from_state=old_state, deny_reason=deny_reason, payload={"reason": reason})
            raise PermissionError(deny_reason)

        self._ensure_scheduler(task_dict)
        self._scheduler_snapshot(task_dict, f"task-action-before-{action}")

        if action == "stop":
            if old_state in TERMINAL_STATES or old_state == TaskState.Blocked.value:
                raise ValueError(f"任务当前状态 {old_state} 不支持 stop")
            task_dict["_prev_state"] = old_state
            task_dict["state"] = TaskState.Blocked.value
            task_dict["block"] = reason or "皇上叫停"
            task_dict["now"] = f"⏸️ 已暂停：{reason or '皇上叫停'}"
        elif action == "cancel":
            if old_state in TERMINAL_STATES:
                raise ValueError(f"任务当前状态 {old_state} 不支持 cancel")
            task_dict["_prev_state"] = old_state
            task_dict["state"] = TaskState.Cancelled.value
            task_dict["org"] = "皇上"
            task_dict["block"] = reason or "皇上取消"
            task_dict["now"] = f"🚫 已取消：{reason or '皇上取消'}"
        elif action == "resume":
            if old_state != TaskState.Blocked.value:
                raise ValueError(f"任务当前状态 {old_state} 不支持 resume")
            previous_state = canonicalize_state(task_dict.get("_prev_state") or "")
            if not previous_state or previous_state in TERMINAL_STATES:
                raise ValueError("Blocked 任务没有可恢复的非终态快照")
            ok, reason_text = ensure_execution_assignment(task_dict, previous_state)
            if not ok:
                raise ValueError(reason_text)
            task_dict["state"] = previous_state
            task_dict["block"] = "无"
            task_dict["now"] = "▶️ 已恢复执行"
        else:
            raise ValueError(f"未知任务动作: {action}")

        task_dict.setdefault("flow_log", []).append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "from": "皇上",
                "to": task_dict.get("org", ""),
                "remark": f"{'⏸️ 叫停' if action == 'stop' else '🚫 取消' if action == 'cancel' else '▶️ 恢复'}：{reason}",
            }
        )
        if action == "resume":
            self._scheduler_mark_progress(task_dict, f"恢复到 {task_dict.get('state', 'Doing')}")
        else:
            self._scheduler_add_flow(task_dict, f"皇上{action}：{reason or '无'}")

        self._bump_state_version(task, task_dict)
        self._apply_task_dict(task, task_dict)
        task.state = TaskState(canonicalize_state(task_dict["state"]))
        task.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self._publish_state_event(task, actor, old_state, reason, transition_kind=action)
        await self._audit(f"task.{action}", actor, True, task_id=task.id, from_state=old_state, to_state=task.state.value, payload={"reason": reason})
        return {"ok": True, "message": f"{task.id} {'已叫停' if action == 'stop' else '已取消' if action == 'cancel' else '已恢复'}"}

    async def review_task(self, task_id: str, action: str, comment: str = "", actor: ActorContext | None = None) -> dict[str, Any]:
        actor = actor or make_actor_context("emperor", source="dashboard")
        task = await self._get_task(task_id, for_update=True)
        task_dict = task.to_dict()
        current_state = canonicalize_state(task_dict.get("state"))
        allowed, deny_reason = authorize_review(actor, task_dict, action)
        if not allowed:
            await self._audit(f"review.{action}", actor, False, task_id=task.id, from_state=current_state, deny_reason=deny_reason, payload={"comment": comment})
            raise PermissionError(deny_reason)

        self._ensure_scheduler(task_dict)
        self._scheduler_snapshot(task_dict, f"review-before-{action}")
        ok, transition = review_transition(task_dict, action, comment)
        if not ok:
            raise ValueError(transition["reason"])

        task_dict["state"] = transition["new_state"]
        task_dict["org"] = transition["to_org"]
        if transition.get("increment_review_round"):
            next_round = int(task_dict.get("review_round") or 0) + 1
            task_dict["review_round"] = next_round
            task_dict["now"] = f"{transition['now']}（第{next_round}轮）"
        else:
            task_dict["now"] = transition["now"]
        task_dict.setdefault("flow_log", []).append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "from": transition["from_org"],
                "to": transition["to_org"],
                "remark": transition["remark"],
            }
        )
        self._scheduler_mark_progress(task_dict, f"审议动作 {action} -> {transition['new_state']}")
        self._bump_state_version(task, task_dict)
        self._apply_task_dict(task, task_dict)
        task.state = TaskState(canonicalize_state(task_dict["state"]))
        task.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self._publish_state_event(task, actor, current_state, comment, transition_kind=f"review.{action}")
        await self._maybe_fast_track_assigned(task)
        await self._audit(f"review.{action}", actor, True, task_id=task.id, from_state=current_state, to_state=task.state.value, payload={"comment": comment})
        return {"ok": True, "message": f"{task.id} {'已准奏' if action == 'approve' else '已驳回'}"}

    async def advance_task(self, task_id: str, comment: str = "", actor: ActorContext | None = None) -> dict[str, Any]:
        actor = actor or make_actor_context("emperor", source="dashboard")
        task = await self._get_task(task_id, for_update=True)
        task_dict = task.to_dict()
        current_state = canonicalize_state(task_dict.get("state"))
        ok, transition = next_manual_transition(task_dict)
        if not ok:
            raise ValueError(transition["reason"])
        next_state = transition["next_state"]
        allowed, deny_reason = authorize_transition(actor, task_dict, next_state)
        if not allowed:
            raise PermissionError(deny_reason)

        self._ensure_scheduler(task_dict)
        self._scheduler_snapshot(task_dict, f"advance-before-{current_state}")
        remark = comment or transition["remark"]
        task_dict["state"] = next_state
        task_dict["org"] = transition["to_org"]
        task_dict["now"] = f"⬇️ 手动推进：{remark}"
        task_dict.setdefault("flow_log", []).append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "from": transition["from_org"],
                "to": transition["to_org"],
                "remark": remark,
            }
        )
        self._scheduler_mark_progress(task_dict, f"手动推进到 {next_state}")
        self._bump_state_version(task, task_dict)
        self._apply_task_dict(task, task_dict)
        task.state = TaskState(canonicalize_state(task_dict["state"]))
        task.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self._publish_state_event(task, actor, current_state, remark, transition_kind="advance")
        await self._maybe_fast_track_assigned(task)
        await self._audit("task.advance", actor, True, task_id=task.id, from_state=current_state, to_state=next_state, payload={"comment": comment})
        return {"ok": True, "message": f"{task.id} 已推进到 {next_state}"}

    async def archive_task(self, task_id: str, archived: bool, actor: ActorContext | None = None) -> dict[str, Any]:
        actor = actor or make_actor_context("emperor", source="dashboard")
        task = await self._get_task(task_id, for_update=True)
        task.archived = bool(archived)
        task.archived_at = datetime.now(timezone.utc) if archived else None
        task.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self._audit("task.archive", actor, True, task_id=task.id, from_state=task.state.value, payload={"archived": archived})
        return {"ok": True, "message": f"{task.id} {'已归档' if archived else '已取消归档'}"}

    async def archive_all_done(self, actor: ActorContext | None = None) -> dict[str, Any]:
        actor = actor or make_actor_context("emperor", source="dashboard")
        tasks = await self.list_tasks(limit=500)
        count = 0
        now = datetime.now(timezone.utc)
        for task in tasks:
            if task.state.value in TERMINAL_STATES and not task.archived:
                task.archived = True
                task.archived_at = now
                task.updated_at = now
                count += 1
        await self.db.commit()
        await self._audit("task.archive_all_done", actor, True, task_id="", payload={"count": count})
        return {"ok": True, "message": f"{count} 道旨意已归档", "count": count}

    async def get_task_activity(self, task_id: str) -> dict[str, Any]:
        task = await self._get_task(task_id)
        task_dict = task.to_dict()
        activity = []
        for entry in task_dict.get("flow_log", []):
            activity.append(
                {
                    "kind": "flow",
                    "at": entry.get("at"),
                    "from": entry.get("from"),
                    "to": entry.get("to"),
                    "remark": entry.get("remark"),
                }
            )
        for entry in task_dict.get("progress_log", []):
            activity.append(
                {
                    "kind": "progress",
                    "at": entry.get("at"),
                    "agent": entry.get("agent"),
                    "text": entry.get("text"),
                }
            )
        for entry in task_dict.get("consultLog", []):
            activity.append(
                {
                    "kind": "consult",
                    "at": entry.get("at"),
                    "from": entry.get("from"),
                    "to": entry.get("to"),
                    "remark": entry.get("note"),
                    "agent": entry.get("to"),
                }
            )
        activity.sort(key=lambda item: str(item.get("at") or ""))
        related_agents = sorted(
            {
                *(entry.get("agent") for entry in task_dict.get("progress_log", []) if entry.get("agent")),
                *(entry.get("to") for entry in task_dict.get("consultLog", []) if entry.get("to")),
            }
        )
        return {
            "ok": True,
            "taskId": task_id,
            "activity": activity,
            "relatedAgents": related_agents,
            "lastActive": task_dict.get("updatedAt"),
        }

    async def get_queue_metrics(self) -> dict[str, Any]:
        tasks = await self.list_tasks(limit=500)
        now_dt = datetime.now(timezone.utc)
        queues: dict[str, dict[str, Any]] = {
            "menxia": {"owner": "menxia", "label": "门下省", "states": [TaskState.Menxia.value], "waiting": 0, "overdue": 0, "fastLane": 0, "oldestWaitSec": 0, "tasks": []},
            "shangshu": {"owner": "shangshu", "label": "尚书省", "states": [TaskState.Assigned.value, TaskState.Review.value], "waiting": 0, "overdue": 0, "fastLane": 0, "oldestWaitSec": 0, "tasks": []},
        }
        for task in tasks:
            task_dict = task.to_dict()
            owner = central_queue_owner(task.state.value)
            if not owner or task.archived:
                continue
            wait_from = self._parse_iso(task_dict.get("updatedAt")) or now_dt
            wait_sec = max(0, int((now_dt - wait_from).total_seconds()))
            sla_sec = queue_sla_seconds(task_dict, task.state.value)
            item = {
                "taskId": task.id,
                "state": task.state.value,
                "lane": task_dict.get("lane", "standard"),
                "waitSec": wait_sec,
                "slaSec": sla_sec,
                "overdue": bool(sla_sec and wait_sec > sla_sec),
            }
            queue = queues[owner]
            queue["waiting"] += 1
            queue["oldestWaitSec"] = max(queue["oldestWaitSec"], wait_sec)
            if item["overdue"]:
                queue["overdue"] += 1
            if item["lane"] == "fast":
                queue["fastLane"] += 1
            queue["tasks"].append(item)

        return {
            "ok": True,
            "queues": queues,
            "checkedAt": now_dt.isoformat(),
        }

    async def get_scheduler_state(self, task_id: str) -> dict[str, Any]:
        task = await self._get_task(task_id)
        task_dict = task.to_dict()
        sched = self._ensure_scheduler(task_dict)
        last_progress = self._parse_iso(sched.get("lastProgressAt") or task_dict.get("updatedAt"))
        stalled_sec = 0
        if last_progress:
            stalled_sec = max(0, int((datetime.now(timezone.utc) - last_progress).total_seconds()))
        return {
            "ok": True,
            "taskId": task_id,
            "state": task.state.value,
            "org": task.org,
            "scheduler": sched,
            "stalledSec": stalled_sec,
            "checkedAt": datetime.now(timezone.utc).isoformat(),
        }

    async def scheduler_retry(self, task_id: str, reason: str = "", actor: ActorContext | None = None) -> dict[str, Any]:
        actor = actor or make_actor_context("sili", source="scheduler")
        allowed, deny_reason = authorize_scheduler(actor, "retry")
        if not allowed:
            raise PermissionError(deny_reason)
        task = await self._get_task(task_id, for_update=True)
        task_dict = task.to_dict()
        state = canonicalize_state(task_dict.get("state"))
        if state in TERMINAL_STATES or state == TaskState.Blocked.value:
            raise ValueError(f"任务 {task.id} 当前状态 {state} 不支持重试")
        sched = self._ensure_scheduler(task_dict)
        sched["retryCount"] = int(sched.get("retryCount") or 0) + 1
        sched["lastRetryAt"] = datetime.now(timezone.utc).isoformat()
        sched["lastDispatchTrigger"] = "scheduler-retry"
        self._scheduler_add_flow(task_dict, f"触发重试第{sched['retryCount']}次：{reason or '超时未推进'}")
        self._apply_task_dict(task, task_dict)
        task.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
        agent = resolve_dispatch_agent(task_dict, state)
        if agent:
            await self._publish_dispatch_request(task, actor, agent, message=f"司礼监调度重试：{reason or '请继续推进任务'}", manual=True)
        await self._audit("scheduler.retry", actor, True, task_id=task.id, from_state=state, to_state=state, payload={"reason": reason, "retryCount": sched["retryCount"]})
        return {"ok": True, "message": f"{task.id} 已触发重试派发", "retryCount": sched["retryCount"]}

    async def scheduler_escalate(self, task_id: str, reason: str = "", actor: ActorContext | None = None) -> dict[str, Any]:
        actor = actor or make_actor_context("sili", source="scheduler")
        allowed, deny_reason = authorize_scheduler(actor, "escalate")
        if not allowed:
            raise PermissionError(deny_reason)
        task = await self._get_task(task_id, for_update=True)
        task_dict = task.to_dict()
        state = canonicalize_state(task_dict.get("state"))
        if state in TERMINAL_STATES:
            raise ValueError(f"任务 {task.id} 已结束，无需升级")
        sched = self._ensure_scheduler(task_dict)
        next_level = min(int(sched.get("escalationLevel") or 0) + 1, 2)
        target = "menxia" if next_level == 1 else "shangshu"
        target_label = "门下省" if next_level == 1 else "尚书省"
        sched["escalationLevel"] = next_level
        sched["lastEscalatedAt"] = datetime.now(timezone.utc).isoformat()
        self._scheduler_add_flow(task_dict, f"升级到{target_label}协调：{reason or '任务停滞'}", to=target_label)
        self._apply_task_dict(task, task_dict)
        task.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self.bus.publish(
            topic=TOPIC_TASK_ESCALATED,
            trace_id=task.id,
            event_type="task.scheduler.escalated",
            producer=actor.actor_id,
            payload={"task_id": task.id, "state": state, "target": target, "reason": reason, "level": next_level, "task": task.to_dict()},
            meta=self._event_meta(actor, task, {"level": next_level}),
            dedupe_key=build_task_escalated_dedupe_key(task.id, int(task.state_version or 1), next_level),
        )
        await self._audit("scheduler.escalate", actor, True, task_id=task.id, from_state=state, to_state=state, target_agent=target, payload={"reason": reason, "level": next_level})
        return {"ok": True, "message": f"{task.id} 已升级至{target_label}", "escalationLevel": next_level}

    async def scheduler_rollback(self, task_id: str, reason: str = "", actor: ActorContext | None = None) -> dict[str, Any]:
        actor = actor or make_actor_context("sili", source="scheduler")
        allowed, deny_reason = authorize_scheduler(actor, "rollback")
        if not allowed:
            raise PermissionError(deny_reason)
        task = await self._get_task(task_id, for_update=True)
        task_dict = task.to_dict()
        sched = self._ensure_scheduler(task_dict)
        snapshot = sched.get("snapshot") or {}
        snap_state = canonicalize_state(snapshot.get("state"))
        if not snap_state:
            raise ValueError(f"任务 {task.id} 无可用回滚快照")
        old_state = canonicalize_state(task_dict.get("state"))
        task_dict["state"] = snap_state
        task_dict["org"] = snapshot.get("org", task_dict.get("org", ""))
        task_dict["now"] = f"↩️ 司礼监调度自动回滚：{reason or '恢复到上个稳定节点'}"
        task_dict["block"] = "无"
        sched["retryCount"] = 0
        sched["escalationLevel"] = 0
        sched["stallSince"] = None
        sched["lastProgressAt"] = datetime.now(timezone.utc).isoformat()
        self._scheduler_add_flow(task_dict, f"执行回滚：{old_state} → {snap_state}，原因：{reason or '停滞恢复'}")
        self._bump_state_version(task, task_dict)
        self._apply_task_dict(task, task_dict)
        task.state = TaskState(canonicalize_state(task_dict["state"]))
        task.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self._publish_state_event(task, actor, old_state, reason or "scheduler rollback", transition_kind="rollback")
        await self._audit("scheduler.rollback", actor, True, task_id=task.id, from_state=old_state, to_state=snap_state, payload={"reason": reason})
        return {"ok": True, "message": f"{task.id} 已回滚到 {snap_state}"}

    async def scheduler_scan(self, threshold_sec: int = 600, actor: ActorContext | None = None) -> dict[str, Any]:
        actor = actor or make_actor_context("sili", source="scheduler")
        allowed, deny_reason = authorize_scheduler(actor, "scan")
        if not allowed:
            raise PermissionError(deny_reason)
        threshold_sec = max(60, int(threshold_sec or 600))
        tasks = await self.list_tasks(limit=500)
        actions: list[dict[str, Any]] = []
        now_dt = datetime.now(timezone.utc)

        for task in tasks:
            if task.state.value in TERMINAL_STATES or task.archived or task.state.value == TaskState.Blocked.value:
                continue
            task_dict = task.to_dict()
            sched = self._ensure_scheduler(task_dict)
            task_threshold = int(sched.get("stallThresholdSec") or threshold_sec)
            last_progress = self._parse_iso(sched.get("lastProgressAt") or task_dict.get("updatedAt"))
            if not last_progress:
                continue
            stalled_sec = max(0, int((now_dt - last_progress).total_seconds()))
            if stalled_sec < task_threshold:
                continue
            if not sched.get("stallSince"):
                sched["stallSince"] = now_dt.isoformat()
            retry_count = int(sched.get("retryCount") or 0)
            max_retry = max(0, int(sched.get("maxRetry") or 1))
            level = int(sched.get("escalationLevel") or 0)

            self._apply_task_dict(task, task_dict)
            task.updated_at = now_dt
            await self.db.commit()
            await self._publish_stalled_event(
                task,
                actor,
                stalled_sec=stalled_sec,
                threshold_sec=task_threshold,
                retry_count=retry_count,
                escalation_level=level,
            )

            if retry_count < max_retry:
                result = await self.scheduler_retry(task.id, reason=f"停滞 {stalled_sec} 秒", actor=actor)
                actions.append({"taskId": task.id, "action": "retry", "stalledSec": stalled_sec, "message": result["message"]})
                continue
            if level < 2:
                result = await self.scheduler_escalate(task.id, reason=f"停滞 {stalled_sec} 秒", actor=actor)
                actions.append({"taskId": task.id, "action": "escalate", "stalledSec": stalled_sec, "message": result["message"]})
                continue
            if sched.get("autoRollback", True):
                snapshot = sched.get("snapshot") or {}
                snap_state = canonicalize_state(snapshot.get("state"))
                if snap_state and snap_state != task.state.value:
                    result = await self.scheduler_rollback(task.id, reason=f"停滞 {stalled_sec} 秒", actor=actor)
                    actions.append({"taskId": task.id, "action": "rollback", "toState": snap_state, "message": result["message"]})

        return {
            "ok": True,
            "thresholdSec": threshold_sec,
            "actions": actions,
            "count": len(actions),
            "checkedAt": datetime.now(timezone.utc).isoformat(),
        }

    async def get_task(self, task_id: str) -> Task:
        return await self._get_task(task_id)

    async def list_tasks(
        self,
        state: TaskState | None = None,
        org: str | None = None,
        priority: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        stmt = select(Task)
        conditions = []
        if state is not None:
            conditions.append(Task.state == state)
        if org:
            conditions.append(Task.org == org)
        if priority:
            conditions.append(Task.priority == priority)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(Task.created_at.desc()).limit(limit).offset(offset)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_live_status(self) -> dict[str, Any]:
        tasks = await self.list_tasks(limit=200)
        queue_metrics = await self.get_queue_metrics()
        return {
            "tasks": [task.to_dict() for task in tasks],
            "syncStatus": {
                "ok": True,
                "engine": "edict-backend",
                "controlPlane": "fastapi",
                "queueMetrics": queue_metrics.get("queues", {}),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
        }

    async def count_tasks(self, state: TaskState | None = None) -> int:
        stmt = select(func.count(Task.id))
        if state is not None:
            stmt = stmt.where(Task.state == state)
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    async def _publish_state_event(
        self,
        task: Task,
        actor: ActorContext,
        from_state: str,
        reason: str,
        *,
        transition_kind: str = "transition",
    ) -> None:
        topic = TOPIC_TASK_COMPLETED if task.state.value in TERMINAL_STATES else TOPIC_TASK_STATUS
        task_payload = task.to_dict()
        await self.bus.publish(
            topic=topic,
            trace_id=task.id,
            event_type=f"task.state.{task.state.value}",
            producer=actor.actor_id,
            payload={
                "task_id": task.id,
                "from": from_state,
                "to": task.state.value,
                "reason": reason,
                "org": task_payload.get("org", ""),
                "targetDept": task_payload.get("targetDept", ""),
                "lane": task_payload.get("lane", "standard"),
                "_stateVersion": task_payload.get("_stateVersion", int(task.state_version or 1)),
                "task": task_payload,
            },
            meta=self._event_meta(actor, task, {"transition_kind": transition_kind}),
            dedupe_key=build_task_state_dedupe_key(task.id, task.state.value, int(task.state_version or 1)),
        )

    async def _publish_stalled_event(
        self,
        task: Task,
        actor: ActorContext,
        *,
        stalled_sec: int,
        threshold_sec: int,
        retry_count: int,
        escalation_level: int,
    ) -> None:
        task_payload = task.to_dict()
        await self.bus.publish(
            topic=TOPIC_TASK_STALLED,
            trace_id=task.id,
            event_type="task.scheduler.stalled",
            producer=actor.actor_id,
            payload={
                "task_id": task.id,
                "state": task.state.value,
                "org": task.org,
                "stalledSec": int(stalled_sec),
                "thresholdSec": int(threshold_sec),
                "retryCount": int(retry_count),
                "escalationLevel": int(escalation_level),
                "task": task_payload,
            },
            meta=self._event_meta(actor, task),
            dedupe_key=build_task_stalled_dedupe_key(task.id, task.state.value, int(task.state_version or 1)),
        )

    async def _publish_dispatch_request(
        self,
        task: Task,
        actor: ActorContext,
        target_agent: str,
        *,
        message: str,
        manual: bool,
    ) -> None:
        task_dict = task.to_dict()
        build_key = build_manual_dispatch_key if manual else build_auto_dispatch_key
        dispatch_key = build_key(
            task.id,
            task.state.value,
            int(task.state_version or 1),
            target_agent,
            request_id=actor.request_id,
        )
        await self.bus.publish(
            topic=TOPIC_TASK_DISPATCH,
            trace_id=task.id,
            event_type="task.dispatch.request",
            producer=actor.actor_id,
            payload={
                "task_id": task.id,
                "agent": target_agent,
                "message": message,
                "state": task.state.value,
                "dispatch_key": dispatch_key,
                "version": int(task.state_version or 1),
                "task": task_dict,
            },
            meta=self._event_meta(actor, task, {"dispatch_key": dispatch_key, "manual": manual}),
            dedupe_key=dispatch_key,
        )

    def _default_scheduler(self, *, updated_at: str) -> dict[str, Any]:
        return {
            "enabled": True,
            "stallThresholdSec": 600,
            "maxRetry": 2,
            "retryCount": 0,
            "escalationLevel": 0,
            "autoRollback": True,
            "lastProgressAt": updated_at,
            "stallSince": None,
            "lastDispatchStatus": "idle",
            "snapshot": {
                "state": TaskState.Sili.value,
                "org": "司礼监",
                "now": "等待司礼监接旨分办",
                "savedAt": updated_at,
                "note": "init",
            },
        }

    async def _maybe_fast_track_assigned(self, task: Task) -> None:
        task_dict = task.to_dict()
        if canonicalize_state(task.state.value) != TaskState.Assigned.value:
            return
        if not fast_lane_ready(task_dict):
            return
        actor = make_actor_context("system", source="fast-lane")
        await self.transition_state(
            task.id,
            TaskState.Next,
            actor=actor,
            reason="快车道任务已具备执行部门，自动进入执行队列",
        )

    def _ensure_scheduler(self, task_dict: dict[str, Any]) -> dict[str, Any]:
        ensure_task_shape(task_dict)
        sched = task_dict.setdefault("_scheduler", {})
        if not isinstance(sched, dict):
            sched = {}
            task_dict["_scheduler"] = sched
        sched.setdefault("enabled", True)
        default_sla = queue_sla_seconds(task_dict, task_dict.get("state"))
        sched.setdefault("stallThresholdSec", default_sla or 600)
        sched.setdefault("maxRetry", 2)
        sched.setdefault("retryCount", 0)
        sched.setdefault("escalationLevel", 0)
        sched.setdefault("autoRollback", True)
        sched.setdefault("lastProgressAt", task_dict.get("updatedAt") or datetime.now(timezone.utc).isoformat())
        sched.setdefault("stallSince", None)
        sched.setdefault("lastDispatchStatus", "idle")
        sched.setdefault(
            "snapshot",
            {
                "state": task_dict.get("state", ""),
                "org": task_dict.get("org", ""),
                "now": task_dict.get("now", ""),
                "savedAt": datetime.now(timezone.utc).isoformat(),
                "note": "init",
            },
        )
        return sched

    def _scheduler_add_flow(self, task_dict: dict[str, Any], remark: str, to: str = "") -> None:
        task_dict.setdefault("flow_log", []).append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "from": "司礼监调度",
                "to": to or task_dict.get("org", ""),
                "remark": f"🧭 {remark}",
            }
        )

    def _scheduler_snapshot(self, task_dict: dict[str, Any], note: str = "") -> None:
        sched = self._ensure_scheduler(task_dict)
        sched["snapshot"] = {
            "state": task_dict.get("state", ""),
            "org": task_dict.get("org", ""),
            "now": task_dict.get("now", ""),
            "savedAt": datetime.now(timezone.utc).isoformat(),
            "note": note or "snapshot",
        }

    def _scheduler_mark_progress(self, task_dict: dict[str, Any], note: str = "") -> None:
        sched = self._ensure_scheduler(task_dict)
        recommended_sla = queue_sla_seconds(task_dict, task_dict.get("state"))
        if recommended_sla:
            sched["stallThresholdSec"] = recommended_sla
        sched["lastProgressAt"] = datetime.now(timezone.utc).isoformat()
        sched["stallSince"] = None
        sched["retryCount"] = 0
        sched["escalationLevel"] = 0
        sched["lastEscalatedAt"] = None
        if note:
            self._scheduler_add_flow(task_dict, f"进展确认：{note}")

    def _event_meta(self, actor: ActorContext, task: Task, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        meta = {
            "source": actor.source,
            "request_id": actor.request_id,
            "version": int(task.state_version or 1),
        }
        if extra:
            meta.update(extra)
        return meta

    def _bump_state_version(self, task: Task, task_dict: dict[str, Any]) -> int:
        next_version = max(1, int(task.state_version or 1)) + 1
        task.state_version = next_version
        task_dict["_stateVersion"] = next_version
        return next_version

    async def _next_task_id(self) -> str:
        today = datetime.now().strftime("%Y%m%d")
        prefix = f"JJC-{today}-"
        stmt = select(Task.id).where(Task.id.like(f"{prefix}%"))
        result = await self.db.execute(stmt)
        ids = [value for value in result.scalars().all() if isinstance(value, str)]
        nums = [int(item.split("-")[-1]) for item in ids if item.split("-")[-1].isdigit()]
        return f"{prefix}{(max(nums) + 1 if nums else 1):03d}"

    async def _get_task(self, task_id: str, *, for_update: bool = False) -> Task:
        if for_update:
            stmt = select(Task).where(Task.id == task_id).with_for_update()
            result = await self.db.execute(stmt)
            task = result.scalar_one_or_none()
        else:
            task = await self.db.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        return task

    def _apply_task_dict(self, task: Task, payload: dict[str, Any]) -> None:
        normalized = dict(payload)
        ensure_task_shape(normalized)
        task.official = normalized.get("official", task.official)
        task.org = normalized.get("org", task.org)
        task.now = normalized.get("now", task.now)
        task.eta = normalized.get("eta", task.eta)
        task.block = normalized.get("block", task.block)
        task.output = normalized.get("output", task.output)
        task.ac = normalized.get("ac", task.ac)
        task.priority = normalized.get("priority", task.priority)
        task.lane = canonicalize_lane(normalized.get("lane") or task.lane or "standard")
        task.review_round = int(normalized.get("review_round") or task.review_round or 0)
        task.state_version = int(normalized.get("_stateVersion") or task.state_version or 1)
        task.archived = bool(normalized.get("archived", task.archived))
        task.flow_log = normalized.get("flow_log", task.flow_log)
        task.progress_log = normalized.get("progress_log", task.progress_log)
        task.consult_log = normalized.get("consultLog", task.consult_log)
        task.todos = normalized.get("todos", task.todos)
        task.template_id = normalized.get("templateId", task.template_id)
        task.template_params = normalized.get("templateParams", task.template_params)
        task.target_dept = normalized.get("targetDept", task.target_dept)
        task.prev_state = normalized.get("_prev_state", task.prev_state)
        task.scheduler = normalized.get("_scheduler", task.scheduler)

    def _parse_iso(self, value: str | None) -> datetime | None:
        raw = (value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _audit(
        self,
        action: str,
        actor: ActorContext,
        allowed: bool,
        *,
        task_id: str,
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
        log.info("task_audit=%s", json.dumps(entry, ensure_ascii=False))
        try:
            await self._persist_task_audit(entry)
        except Exception:
            log.exception("task audit persistence failed action=%s task_id=%s", action, task_id)
        return entry

    async def _persist_task_audit(self, entry: dict[str, Any]) -> None:
        try:
            from ..db import async_session
            from ..models.task_audit import TaskAudit
        except Exception:
            return

        async with async_session() as audit_db:
            record = TaskAudit(
                audit_id=uuid.UUID(str(entry["audit_id"])),
                ts=self._parse_iso(entry.get("ts")) or datetime.now(timezone.utc),
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
            audit_db.add(record)
            await audit_db.commit()
