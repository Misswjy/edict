"""add durable task audit table

Revision ID: 002_task_audits
Revises: 001_initial
Create Date: 2026-03-24 00:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "002_task_audits"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_audits",
        sa.Column("audit_id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("request_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("task_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False, server_default="agent"),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("signature_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("from_state", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("to_state", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("target_agent", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("allowed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deny_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("policy_version", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("payload_hash", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("payload_summary", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index("ix_task_audits_ts", "task_audits", ["ts"])
    op.create_index("ix_task_audits_request_id", "task_audits", ["request_id"])
    op.create_index("ix_task_audits_task_id", "task_audits", ["task_id"])
    op.create_index("ix_task_audits_action", "task_audits", ["action"])
    op.create_index("ix_task_audits_actor_id", "task_audits", ["actor_id"])
    op.create_index("ix_task_audits_source", "task_audits", ["source"])
    op.create_index("ix_task_audits_allowed", "task_audits", ["allowed"])
    op.create_index("ix_task_audits_task_action_ts", "task_audits", ["task_id", "action", "ts"])
    op.create_index("ix_task_audits_actor_action", "task_audits", ["actor_id", "action"])
    op.create_index("ix_task_audits_allowed_ts", "task_audits", ["allowed", "ts"])


def downgrade() -> None:
    op.drop_index("ix_task_audits_allowed_ts", table_name="task_audits")
    op.drop_index("ix_task_audits_actor_action", table_name="task_audits")
    op.drop_index("ix_task_audits_task_action_ts", table_name="task_audits")
    op.drop_index("ix_task_audits_allowed", table_name="task_audits")
    op.drop_index("ix_task_audits_source", table_name="task_audits")
    op.drop_index("ix_task_audits_actor_id", table_name="task_audits")
    op.drop_index("ix_task_audits_action", table_name="task_audits")
    op.drop_index("ix_task_audits_task_id", table_name="task_audits")
    op.drop_index("ix_task_audits_request_id", table_name="task_audits")
    op.drop_index("ix_task_audits_ts", table_name="task_audits")
    op.drop_table("task_audits")
