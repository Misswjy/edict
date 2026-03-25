"""Compatibility API for officials statistics."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models.task import Task
from ..services.officials_service import build_officials_payload

router = APIRouter()


@router.get("/officials-stats")
async def officials_stats(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Task))
    tasks = [task.to_dict() for task in result.scalars().all()]
    return build_officials_payload(tasks)
