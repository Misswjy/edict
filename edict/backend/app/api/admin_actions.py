"""Compatibility write APIs for legacy control actions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_control_plane_access
from ..task_contract import make_actor_context
from ..services.admin_action_service import AdminActionService

router = APIRouter(dependencies=[Depends(require_control_plane_access)])


class ActorPayload(BaseModel):
    actor: str = "emperor"
    source: str = "dashboard"
    request_id: str | None = None
    signature: str = ""
    timestamp: str = ""


class SetModelPayload(ActorPayload):
    agentId: str
    model: str


class SetDispatchChannelPayload(ActorPayload):
    channel: str


class AgentWakePayload(ActorPayload):
    agentId: str
    message: str = ""
    taskId: str = ""


def _actor(payload: ActorPayload):
    return make_actor_context(
        payload.actor,
        source=payload.source,
        request_id=payload.request_id,
        signature=payload.signature,
        timestamp=payload.timestamp,
    )


def get_admin_action_service() -> AdminActionService:
    return AdminActionService()


@router.post("/set-model")
async def set_model(body: SetModelPayload, svc: AdminActionService = Depends(get_admin_action_service)):
    result = await svc.set_model(body.agentId, body.model, _actor(body))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "set-model failed"))
    return result


@router.post("/set-dispatch-channel")
async def set_dispatch_channel(body: SetDispatchChannelPayload, svc: AdminActionService = Depends(get_admin_action_service)):
    result = await svc.set_dispatch_channel(body.channel, _actor(body))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "set-dispatch-channel failed"))
    return result


@router.post("/agent-wake")
async def agent_wake(body: AgentWakePayload, svc: AdminActionService = Depends(get_admin_action_service)):
    result = await svc.wake_agent(body.agentId, body.message, _actor(body), task_id=body.taskId)
    if not result.get("ok"):
        raise HTTPException(status_code=400 if "非法" not in result.get("error", "") else 403, detail=result.get("error", "agent-wake failed"))
    return result
