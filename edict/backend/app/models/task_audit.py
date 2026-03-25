"""Durable task audit trail for control-plane actions and permission checks."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from ..db import Base


class TaskAudit(Base):
    __tablename__ = "task_audits"

    audit_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ts = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    request_id = Column(String(64), nullable=False, default="", index=True)
    task_id = Column(String(64), nullable=False, default="", index=True)
    action = Column(String(128), nullable=False, index=True)
    actor_id = Column(String(64), nullable=False, index=True)
    actor_type = Column(String(32), nullable=False, default="agent")
    source = Column(String(64), nullable=False, default="unknown", index=True)
    signature_verified = Column(Boolean, nullable=False, default=False)
    from_state = Column(String(32), nullable=False, default="")
    to_state = Column(String(32), nullable=False, default="")
    target_agent = Column(String(64), nullable=False, default="")
    allowed = Column(Boolean, nullable=False, default=False, index=True)
    deny_reason = Column(Text, nullable=False, default="")
    policy_version = Column(String(32), nullable=False, default="")
    payload = Column(JSONB, nullable=False, default=dict)
    payload_hash = Column(String(16), nullable=False, default="")
    payload_summary = Column(Text, nullable=False, default="")

    __table_args__ = (
        Index("ix_task_audits_task_action_ts", "task_id", "action", "ts"),
        Index("ix_task_audits_actor_action", "actor_id", "action"),
        Index("ix_task_audits_allowed_ts", "allowed", "ts"),
    )

    def to_dict(self) -> dict:
        return {
            "audit_id": str(self.audit_id),
            "ts": self.ts.isoformat() if self.ts else "",
            "request_id": self.request_id,
            "task_id": self.task_id,
            "action": self.action,
            "actor_id": self.actor_id,
            "actor_type": self.actor_type,
            "source": self.source,
            "signature_verified": self.signature_verified,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "target_agent": self.target_agent,
            "allowed": self.allowed,
            "deny_reason": self.deny_reason,
            "policy_version": self.policy_version,
            "payload": self.payload or {},
            "payload_hash": self.payload_hash,
            "payload_summary": self.payload_summary,
        }
