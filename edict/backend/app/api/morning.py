"""Compatibility APIs for the morning-brief panel."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_control_plane_access
from ..task_contract import make_actor_context
from ..services.morning_service import MorningService, get_morning_brief, get_morning_config

router = APIRouter()


class ActorPayload(BaseModel):
    actor: str = "emperor"
    source: str = "dashboard"
    request_id: str | None = None
    signature: str = ""
    timestamp: str = ""


class CategoryConfig(BaseModel):
    name: str
    enabled: bool = True


class CustomFeedConfig(BaseModel):
    name: str
    url: str
    category: str


class MorningConfigPayload(ActorPayload):
    categories: list[CategoryConfig] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    custom_feeds: list[CustomFeedConfig] = Field(default_factory=list)
    feishu_webhook: str = ""


class MorningRefreshPayload(ActorPayload):
    force: bool = True


def _actor(payload: ActorPayload):
    return make_actor_context(
        payload.actor,
        source=payload.source,
        request_id=payload.request_id,
        signature=payload.signature,
        timestamp=payload.timestamp,
    )


def get_morning_service() -> MorningService:
    return MorningService()


@router.get("/morning-brief")
async def morning_brief():
    return await get_morning_brief()


@router.get("/morning-brief/{date}")
async def morning_brief_for_date(date: str):
    return await get_morning_brief(date=date)


@router.get("/morning-config")
async def morning_config():
    return await get_morning_config()


@router.post("/morning-config", dependencies=[Depends(require_control_plane_access)])
async def save_morning_config(body: MorningConfigPayload, svc: MorningService = Depends(get_morning_service)):
    result = await svc.save_config(body.model_dump(exclude={"actor", "source", "request_id", "signature", "timestamp"}), _actor(body))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "save-morning-config failed"))
    return result


@router.post("/morning-brief/refresh", dependencies=[Depends(require_control_plane_access)])
async def refresh_morning_brief(body: MorningRefreshPayload, svc: MorningService = Depends(get_morning_service)):
    result = await svc.refresh_brief(_actor(body), force=body.force)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "refresh-morning-brief failed"))
    return result
