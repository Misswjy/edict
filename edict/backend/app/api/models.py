"""Compatibility read APIs for model/runtime metadata."""

from __future__ import annotations

from fastapi import APIRouter

from ..services.agent_config_service import get_model_change_log

router = APIRouter()


@router.get("/model-change-log")
async def model_change_log():
    return await get_model_change_log()
