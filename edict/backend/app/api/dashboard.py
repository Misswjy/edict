"""Dashboard compatibility routes backed by the FastAPI control plane."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_control_plane_access
from .tasks import get_task_service
from ..task_contract import make_actor_context
from ..services.task_service import TaskService

router = APIRouter()


class ActorPayload(BaseModel):
    actor: str = "emperor"
    source: str = "dashboard"
    request_id: str | None = None
    signature: str = ""
    timestamp: str = ""


class CreateTaskPayload(ActorPayload):
    title: str
    org: str = "中书省"
    official: str = "中书令"
    priority: str = "normal"
    lane: str = "standard"
    templateId: str = ""
    params: dict = Field(default_factory=dict)
    targetDept: str = ""


class TaskActionPayload(ActorPayload):
    taskId: str
    action: str
    reason: str = ""


class ReviewPayload(ActorPayload):
    taskId: str
    action: str
    comment: str = ""


class AdvancePayload(ActorPayload):
    taskId: str
    comment: str = ""


class ArchivePayload(ActorPayload):
    taskId: str = ""
    archived: bool = True
    archiveAllDone: bool = False


class TodoPayload(ActorPayload):
    taskId: str
    todos: list[dict]


class SchedulerPayload(ActorPayload):
    taskId: str
    reason: str = ""


class SchedulerScanPayload(ActorPayload):
    thresholdSec: int = 180


class ConsultPayload(ActorPayload):
    taskId: str
    targetAgent: str
    note: str = ""


def _actor(payload: ActorPayload):
    return make_actor_context(
        payload.actor,
        source=payload.source,
        request_id=payload.request_id,
        signature=payload.signature,
        timestamp=payload.timestamp,
    )


@router.get("/live-status")
async def live_status(svc: TaskService = Depends(get_task_service)):
    return await svc.get_live_status()


@router.get("/task-activity/{task_id}")
async def task_activity(task_id: str, svc: TaskService = Depends(get_task_service)):
    try:
        return await svc.get_task_activity(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/scheduler-state/{task_id}")
async def scheduler_state(task_id: str, svc: TaskService = Depends(get_task_service)):
    try:
        return await svc.get_scheduler_state(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/queue-metrics")
async def queue_metrics(svc: TaskService = Depends(get_task_service)):
    return await svc.get_queue_metrics()


@router.post("/create-task", dependencies=[Depends(require_control_plane_access)])
async def create_task(body: CreateTaskPayload, svc: TaskService = Depends(get_task_service)):
    task = await svc.create_task(
        title=body.title,
        official=body.official,
        priority=body.priority,
        lane=body.lane,
        template_id=body.templateId,
        template_params=body.params,
        target_dept=body.targetDept,
        actor=_actor(body),
    )
    return {"ok": True, "taskId": task.id, "message": f"旨意 {task.id} 已下达，正在派发给司礼监"}


@router.post("/task-action", dependencies=[Depends(require_control_plane_access)])
async def task_action(body: TaskActionPayload, svc: TaskService = Depends(get_task_service)):
    try:
        return await svc.task_action(body.taskId, body.action, body.reason, actor=_actor(body))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/review-action", dependencies=[Depends(require_control_plane_access)])
async def review_action(body: ReviewPayload, svc: TaskService = Depends(get_task_service)):
    try:
        return await svc.review_task(body.taskId, body.action, body.comment, actor=_actor(body))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/advance-state", dependencies=[Depends(require_control_plane_access)])
async def advance_state(body: AdvancePayload, svc: TaskService = Depends(get_task_service)):
    try:
        return await svc.advance_task(body.taskId, body.comment, actor=_actor(body))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/task-consult", dependencies=[Depends(require_control_plane_access)])
async def task_consult(body: ConsultPayload, svc: TaskService = Depends(get_task_service)):
    try:
        return await svc.request_consultation(body.taskId, body.targetAgent, note=body.note, actor=_actor(body))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/archive-task", dependencies=[Depends(require_control_plane_access)])
async def archive_task(body: ArchivePayload, svc: TaskService = Depends(get_task_service)):
    try:
        if body.archiveAllDone:
            return await svc.archive_all_done(actor=_actor(body))
        return await svc.archive_task(body.taskId, body.archived, actor=_actor(body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/task-todos", dependencies=[Depends(require_control_plane_access)])
async def task_todos(body: TodoPayload, svc: TaskService = Depends(get_task_service)):
    try:
        await svc.update_todos(body.taskId, actor=_actor(body), todos=body.todos)
        return {"ok": True, "message": "todos 已更新"}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/scheduler-scan", dependencies=[Depends(require_control_plane_access)])
async def scheduler_scan(body: SchedulerScanPayload, svc: TaskService = Depends(get_task_service)):
    try:
        return await svc.scheduler_scan(body.thresholdSec, actor=_actor(body))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/scheduler-retry", dependencies=[Depends(require_control_plane_access)])
async def scheduler_retry(body: SchedulerPayload, svc: TaskService = Depends(get_task_service)):
    try:
        return await svc.scheduler_retry(body.taskId, body.reason, actor=_actor(body))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/scheduler-escalate", dependencies=[Depends(require_control_plane_access)])
async def scheduler_escalate(body: SchedulerPayload, svc: TaskService = Depends(get_task_service)):
    try:
        return await svc.scheduler_escalate(body.taskId, body.reason, actor=_actor(body))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/scheduler-rollback", dependencies=[Depends(require_control_plane_access)])
async def scheduler_rollback(body: SchedulerPayload, svc: TaskService = Depends(get_task_service)):
    try:
        return await svc.scheduler_rollback(body.taskId, body.reason, actor=_actor(body))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
