"""Compatibility APIs for court discussion sessions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_control_plane_access
from ..services.court_discuss_service import CourtDiscussService
from ..task_contract import make_actor_context

router = APIRouter()


class ActorPayload(BaseModel):
    actor: str = "emperor"
    source: str = "dashboard"
    request_id: str | None = None
    signature: str = ""
    timestamp: str = ""


class CourtDiscussStartPayload(ActorPayload):
    topic: str
    officials: list[str] = Field(default_factory=list)
    taskId: str = ""


class CourtDiscussAdvancePayload(ActorPayload):
    sessionId: str
    userMessage: str = ""
    decree: str = ""


class CourtDiscussSessionPayload(ActorPayload):
    sessionId: str


def _actor(payload: ActorPayload):
    return make_actor_context(
        payload.actor,
        source=payload.source,
        request_id=payload.request_id,
        signature=payload.signature,
        timestamp=payload.timestamp,
    )


def get_court_discuss_service() -> CourtDiscussService:
    return CourtDiscussService()


@router.get("/court-discuss/list")
async def court_discuss_list(svc: CourtDiscussService = Depends(get_court_discuss_service)):
    return {"ok": True, "sessions": await svc.list_sessions()}


@router.get("/court-discuss/officials")
async def court_discuss_officials(svc: CourtDiscussService = Depends(get_court_discuss_service)):
    return await svc.get_officials_payload()


@router.get("/court-discuss/session/{session_id}")
async def court_discuss_session(session_id: str, svc: CourtDiscussService = Depends(get_court_discuss_service)):
    result = await svc.get_session(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="session not found")
    return result


@router.get("/court-discuss/fate")
async def court_discuss_fate(svc: CourtDiscussService = Depends(get_court_discuss_service)):
    return await svc.get_fate_payload()


@router.post("/court-discuss/start", dependencies=[Depends(require_control_plane_access)])
async def court_discuss_start(body: CourtDiscussStartPayload, svc: CourtDiscussService = Depends(get_court_discuss_service)):
    result = await svc.create_session(body.topic, body.officials, body.taskId, _actor(body))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "court-discuss-start failed"))
    return result


@router.post("/court-discuss/advance", dependencies=[Depends(require_control_plane_access)])
async def court_discuss_advance(body: CourtDiscussAdvancePayload, svc: CourtDiscussService = Depends(get_court_discuss_service)):
    result = await svc.advance_discussion(
        body.sessionId,
        body.userMessage.strip() or None,
        body.decree.strip() or None,
        _actor(body),
    )
    if not result.get("ok"):
        status_code = 404 if "不存在" in str(result.get("error") or "") else 400
        raise HTTPException(status_code=status_code, detail=result.get("error", "court-discuss-advance failed"))
    return result


@router.post("/court-discuss/conclude", dependencies=[Depends(require_control_plane_access)])
async def court_discuss_conclude(body: CourtDiscussSessionPayload, svc: CourtDiscussService = Depends(get_court_discuss_service)):
    result = await svc.conclude_session(body.sessionId, _actor(body))
    if not result.get("ok"):
        status_code = 404 if "不存在" in str(result.get("error") or "") else 400
        raise HTTPException(status_code=status_code, detail=result.get("error", "court-discuss-conclude failed"))
    return result


@router.post("/court-discuss/destroy", dependencies=[Depends(require_control_plane_access)])
async def court_discuss_destroy(body: CourtDiscussSessionPayload, svc: CourtDiscussService = Depends(get_court_discuss_service)):
    return await svc.destroy_session(body.sessionId, _actor(body))
