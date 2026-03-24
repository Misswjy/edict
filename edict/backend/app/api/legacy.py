"""Legacy routes kept for compatibility with string task IDs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..task_contract import TaskState, canonicalize_state, make_actor_context
from ..services.event_bus import get_event_bus
from ..services.task_service import TaskService

router = APIRouter()


class LegacyActor(BaseModel):
    actor: str = "system"
    source: str = "legacy"
    request_id: str | None = None
    signature: str = ""
    timestamp: str = ""


class LegacyTransition(LegacyActor):
    new_state: str
    reason: str = ""


class LegacyProgress(LegacyActor):
    content: str


class LegacyTodoUpdate(LegacyActor):
    todos: list[dict]


def _actor(payload: LegacyActor):
    return make_actor_context(
        payload.actor,
        source=payload.source,
        request_id=payload.request_id,
        signature=payload.signature,
        timestamp=payload.timestamp,
    )


async def _service(db: AsyncSession) -> TaskService:
    bus = await get_event_bus()
    return TaskService(db, bus)


@router.post("/by-legacy/{legacy_id}/transition")
async def legacy_transition(legacy_id: str, body: LegacyTransition, db: AsyncSession = Depends(get_db)):
    svc = await _service(db)
    try:
        new_state = TaskState(canonicalize_state(body.new_state))
        task = await svc.transition_state(legacy_id, new_state, actor=_actor(body), reason=body.reason)
        return {"taskId": task.id, "state": task.state.value}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/by-legacy/{legacy_id}/progress")
async def legacy_progress(legacy_id: str, body: LegacyProgress, db: AsyncSession = Depends(get_db)):
    svc = await _service(db)
    try:
        await svc.add_progress(legacy_id, _actor(body), body.content)
        return {"message": "ok"}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/by-legacy/{legacy_id}/todos")
async def legacy_todos(legacy_id: str, body: LegacyTodoUpdate, db: AsyncSession = Depends(get_db)):
    svc = await _service(db)
    try:
        await svc.update_todos(legacy_id, _actor(body), body.todos)
        return {"message": "ok"}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/by-legacy/{legacy_id}")
async def legacy_get(legacy_id: str, db: AsyncSession = Depends(get_db)):
    svc = await _service(db)
    try:
        task = await svc.get_task(legacy_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Legacy task not found: {legacy_id}")
    return task.to_dict()
