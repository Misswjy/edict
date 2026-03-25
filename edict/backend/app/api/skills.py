"""Compatibility read APIs for local/remote skill metadata."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_control_plane_access
from ..task_contract import make_actor_context
from ..services.agent_config_service import list_remote_skills, read_skill_content
from ..services.skills_service import SkillsService

router = APIRouter()


class ActorPayload(BaseModel):
    actor: str = "emperor"
    source: str = "dashboard"
    request_id: str | None = None
    signature: str = ""
    timestamp: str = ""


class AddSkillPayload(ActorPayload):
    agentId: str
    skillName: str
    description: str = ""
    trigger: str = ""


class RemoteSkillPayload(ActorPayload):
    agentId: str
    skillName: str


class AddRemoteSkillPayload(RemoteSkillPayload):
    sourceUrl: str
    description: str = ""


def _actor(payload: ActorPayload):
    return make_actor_context(
        payload.actor,
        source=payload.source,
        request_id=payload.request_id,
        signature=payload.signature,
        timestamp=payload.timestamp,
    )


def get_skills_service() -> SkillsService:
    return SkillsService()


@router.get("/skill-content/{agent_id}/{skill_name}")
async def skill_content(agent_id: str, skill_name: str):
    return read_skill_content(agent_id, skill_name)


@router.get("/remote-skills-list")
async def remote_skills_list():
    return list_remote_skills()


@router.post("/add-skill", dependencies=[Depends(require_control_plane_access)])
async def add_skill(body: AddSkillPayload, svc: SkillsService = Depends(get_skills_service)):
    result = await svc.add_skill_to_agent(body.agentId, body.skillName, body.description, body.trigger, _actor(body))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "add-skill failed"))
    return result


@router.post("/add-remote-skill", dependencies=[Depends(require_control_plane_access)])
async def add_remote_skill(body: AddRemoteSkillPayload, svc: SkillsService = Depends(get_skills_service)):
    result = await svc.add_remote_skill(body.agentId, body.skillName, body.sourceUrl, body.description, _actor(body))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "add-remote-skill failed"))
    return result


@router.post("/update-remote-skill", dependencies=[Depends(require_control_plane_access)])
async def update_remote_skill(body: RemoteSkillPayload, svc: SkillsService = Depends(get_skills_service)):
    result = await svc.update_remote_skill(body.agentId, body.skillName, _actor(body))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "update-remote-skill failed"))
    return result


@router.post("/remove-remote-skill", dependencies=[Depends(require_control_plane_access)])
async def remove_remote_skill(body: RemoteSkillPayload, svc: SkillsService = Depends(get_skills_service)):
    result = await svc.remove_remote_skill(body.agentId, body.skillName, _actor(body))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "remove-remote-skill failed"))
    return result
