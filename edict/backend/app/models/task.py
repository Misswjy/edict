"""Task ORM model aligned with the shared task contract."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from ..db import Base
from ..task_contract import TaskState


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(64), primary_key=True, comment="任务ID, e.g. JJC-20260324-001")
    title = Column(Text, nullable=False, comment="任务标题")
    official = Column(String(64), nullable=False, default="", comment="责任官员/职位")
    org = Column(String(64), nullable=False, default="皇上", comment="当前负责部门")
    state = Column(Enum(TaskState, name="task_state"), nullable=False, default=TaskState.Pending, index=True)

    now = Column(Text, default="", comment="当前进展描述")
    eta = Column(String(64), default="-", comment="预计完成时间")
    block = Column(Text, default="无", comment="阻塞原因")
    output = Column(Text, default="", comment="最终产出")
    ac = Column(Text, default="", comment="验收标准")
    priority = Column(String(16), default="normal", comment="优先级")
    lane = Column(String(16), default="standard", nullable=False, comment="任务车道: standard|fast")
    review_round = Column(Integer, default=0, nullable=False, comment="门下封驳轮次")
    state_version = Column(Integer, default=1, nullable=False, comment="状态版本，用于派发幂等")
    archived = Column(Boolean, default=False, index=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)

    flow_log = Column(JSONB, default=list, comment="流转日志")
    progress_log = Column(JSONB, default=list, comment="进度日志")
    consult_log = Column(JSONB, default=list, comment="横向咨询日志")
    todos = Column(JSONB, default=list, comment="子任务")
    scheduler = Column(JSONB, default=dict, comment="调度状态")

    template_id = Column(String(64), default="", comment="模板ID")
    template_params = Column(JSONB, default=dict, comment="模板参数")
    target_dept = Column(String(64), default="", comment="目标执行部门")
    prev_state = Column(String(32), default="", comment="阻塞前状态")

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_tasks_state_archived", "state", "archived"),
        Index("ix_tasks_updated_at", "updated_at"),
        Index("ix_tasks_org_state", "org", "state"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "official": self.official,
            "org": self.org,
            "state": self.state.value if self.state else "",
            "now": self.now,
            "eta": self.eta,
            "block": self.block,
            "output": self.output,
            "ac": self.ac,
            "priority": self.priority,
            "lane": self.lane or "standard",
            "review_round": self.review_round,
            "archived": self.archived,
            "archivedAt": self.archived_at.isoformat() if self.archived_at else None,
            "flow_log": self.flow_log or [],
            "progress_log": self.progress_log or [],
            "consultLog": self.consult_log or [],
            "todos": self.todos or [],
            "templateId": self.template_id,
            "templateParams": self.template_params or {},
            "targetDept": self.target_dept,
            "_prev_state": self.prev_state or "",
            "_stateVersion": int(self.state_version or 1),
            "_scheduler": self.scheduler or {},
            "createdAt": self.created_at.isoformat() if self.created_at else "",
            "updatedAt": self.updated_at.isoformat() if self.updated_at else "",
        }
