"""Tasks API aligned with the shared task contract."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..task_contract import TaskState, canonicalize_state, make_actor_context
from ..services.event_bus import get_event_bus
from ..services.task_service import TaskService

log = logging.getLogger("edict.api.tasks")
router = APIRouter()


class ActorPayload(BaseModel):
    actor: str = "system"
    source: str = "api"
    request_id: str | None = None
    signature: str = ""
    timestamp: str = ""


class TaskCreate(ActorPayload):
    title: str
    official: str = "中书令"
    priority: str = "normal"
    templateId: str = ""
    templateParams: dict = Field(default_factory=dict)
    targetDept: str = ""


class TaskTransition(ActorPayload):
    new_state: str
    reason: str = ""


class TaskDispatch(ActorPayload):
    target_agent: str
    message: str = ""


class TaskProgress(ActorPayload):
    content: str


class TaskTodoUpdate(ActorPayload):
    todos: list[dict]


class TaskSchedulerUpdate(ActorPayload):
    scheduler: dict


async def get_task_service(
    db: AsyncSession = Depends(get_db),
) -> TaskService:
    bus = await get_event_bus()
    return TaskService(db, bus)


def _actor(payload: ActorPayload):
    return make_actor_context(
        payload.actor,
        source=payload.source,
        request_id=payload.request_id,
        signature=payload.signature,
        timestamp=payload.timestamp,
    )


@router.get("")
async def list_tasks(
    state: str | None = None,
    org: str | None = None,
    priority: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    svc: TaskService = Depends(get_task_service),
):
    try:
        task_state = TaskState(canonicalize_state(state)) if state else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    tasks = await svc.list_tasks(state=task_state, org=org, priority=priority, limit=limit, offset=offset)
    return {"tasks": [task.to_dict() for task in tasks], "count": len(tasks)}


@router.get("/live-status")
async def live_status(svc: TaskService = Depends(get_task_service)):
    return await svc.get_live_status()


@router.get("/stats")
async def task_stats(svc: TaskService = Depends(get_task_service)):
    stats = {}
    for state in TaskState:
        stats[state.value] = await svc.count_tasks(state)
    return {"total": sum(stats.values()), "by_state": stats}


@router.post("", status_code=201)
async def create_task(body: TaskCreate, svc: TaskService = Depends(get_task_service)):
    task = await svc.create_task(
        title=body.title,
        official=body.official,
        priority=body.priority,
        template_id=body.templateId,
        template_params=body.templateParams,
        target_dept=body.targetDept,
        actor=_actor(body),
    )
    return {"taskId": task.id, "state": task.state.value}


@router.get("/{task_id}")
async def get_task(task_id: str, svc: TaskService = Depends(get_task_service)):
    try:
        task = await svc.get_task(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.to_dict()


@router.post("/{task_id}/transition")
async def transition_task(task_id: str, body: TaskTransition, svc: TaskService = Depends(get_task_service)):
    try:
        new_state = TaskState(canonicalize_state(body.new_state))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid state: {body.new_state}")

    try:
        task = await svc.transition_state(task_id=task_id, new_state=new_state, actor=_actor(body), reason=body.reason)
        return {"taskId": task.id, "state": task.state.value, "message": "ok"}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{task_id}/dispatch")
async def dispatch_task(task_id: str, body: TaskDispatch, svc: TaskService = Depends(get_task_service)):
    try:
        await svc.request_dispatch(task_id, body.target_agent, actor=_actor(body), message=body.message)
        return {"message": "dispatch requested", "agent": body.target_agent}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{task_id}/progress")
async def add_progress(task_id: str, body: TaskProgress, svc: TaskService = Depends(get_task_service)):
    try:
        await svc.add_progress(task_id, _actor(body), body.content)
        return {"message": "ok"}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/{task_id}/todos")
async def update_todos(task_id: str, body: TaskTodoUpdate, svc: TaskService = Depends(get_task_service)):
    try:
        await svc.update_todos(task_id, _actor(body), body.todos)
        return {"message": "ok"}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/{task_id}/scheduler")
async def update_scheduler(task_id: str, body: TaskSchedulerUpdate, svc: TaskService = Depends(get_task_service)):
    try:
        await svc.update_scheduler(task_id, _actor(body), body.scheduler)
        return {"message": "ok"}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
